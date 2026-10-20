/*
  Weiss is a UCI compliant chess engine.
  Copyright (C) 2019-2026 Terje Kirstihagen

  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <pthread.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#ifdef __linux__
#include <limits.h>
#include <sched.h>
#endif

#include "movegen.h"
#include "threads.h"


Thread *Threads;
static pthread_t *pthreads;

// Used for letting the main thread sleep without using cpu
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t sleepCondition = PTHREAD_COND_INITIALIZER;
static void CheckThreadCall(int error, const char *operation);

#ifdef __linux__
static int *workerCPUs;
static int workerCPUCount;

static void FailCPUBinding(const char *message) {
    fprintf(stderr, "CPU binding failed: %s\n", message);
    exit(EXIT_FAILURE);
}

static void ReadWorkerCPUs(int threadCount) {
    free(workerCPUs);
    workerCPUs = NULL;
    workerCPUCount = 0;

    const char *list = getenv("WEISS_CPU_LIST");
    if (!list || !*list) return;

    const char *cursor = list;
    while (*cursor) {
        errno = 0;
        char *end;
        long cpu = strtol(cursor, &end, 10);
        if (errno || end == cursor || cpu < 0 || cpu >= CPU_SETSIZE
                || (*end && *end != ','))
            FailCPUBinding("WEISS_CPU_LIST must be a comma-separated list of valid CPU IDs");

        for (int i = 0; i < workerCPUCount; ++i)
            if (workerCPUs[i] == cpu)
                FailCPUBinding("WEISS_CPU_LIST contains a duplicate CPU ID");

        int *newList = realloc(workerCPUs, (size_t)(workerCPUCount + 1) * sizeof(*workerCPUs));
        if (!newList) FailCPUBinding("could not allocate the CPU map");
        workerCPUs = newList;
        workerCPUs[workerCPUCount++] = (int)cpu;

        if (*end == ',') {
            cursor = end + 1;
            if (!*cursor) FailCPUBinding("WEISS_CPU_LIST has a trailing comma");
        } else {
            cursor = end;
        }
    }

    if (workerCPUCount < threadCount)
        FailCPUBinding("the CPU map has fewer CPUs than the requested worker count");

    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0)
        FailCPUBinding("could not read the process's allowed CPU set");
    for (int i = 0; i < workerCPUCount; ++i)
        if (!CPU_ISSET(workerCPUs[i], &allowed))
            FailCPUBinding("WEISS_CPU_LIST includes a CPU outside the process's allowed CPU set");
}
#else
static void ReadWorkerCPUs(int threadCount) {
    (void)threadCount;
    const char *list = getenv("WEISS_CPU_LIST");
    if (list && *list) {
        fprintf(stderr, "CPU binding failed: WEISS_CPU_LIST is supported only on Linux.\n");
        exit(EXIT_FAILURE);
    }
}
#endif

static void CreateWorker(int index, void *(*func)(void *), void *argument) {
#ifdef __linux__
    if (workerCPUCount) {
        pthread_attr_t attributes;
        CheckThreadCall(pthread_attr_init(&attributes), "pthread_attr_init");
        cpu_set_t selected;
        CPU_ZERO(&selected);
        CPU_SET(workerCPUs[index], &selected);
        CheckThreadCall(pthread_attr_setaffinity_np(&attributes, sizeof(selected), &selected),
                        "pthread_attr_setaffinity_np");
        int createError = pthread_create(&pthreads[index], &attributes, func, argument);
        int destroyError = pthread_attr_destroy(&attributes);
        CheckThreadCall(createError, "pthread_create");
        CheckThreadCall(destroyError, "pthread_attr_destroy");
        return;
    }
#endif
    CheckThreadCall(pthread_create(&pthreads[index], NULL, func, argument), "pthread_create");
}

bool ThreadBindingEnabled(void) {
#ifdef __linux__
    return workerCPUCount > 0;
#else
    return false;
#endif
}

int ThreadCPUForIndex(int index) {
#ifdef __linux__
    return index >= 0 && index < workerCPUCount ? workerCPUs[index] : -1;
#else
    (void)index;
    return -1;
#endif
}

#ifdef __linux__
typedef struct {
    cpu_set_t allowed;
    int expectedCPU;
    int runningCPU;
    int error;
    int allowedCount;
    bool passed;
} AffinityCheckResult;

static AffinityCheckResult *affinityCheckResults;

static void *InspectWorkerAffinity(void *argument) {
    Thread *thread = argument;
    AffinityCheckResult *result = &affinityCheckResults[thread->index];
    result->expectedCPU = workerCPUs[thread->index];
    result->runningCPU = sched_getcpu();
    CPU_ZERO(&result->allowed);
    result->error = pthread_getaffinity_np(pthread_self(), sizeof(result->allowed), &result->allowed);
    if (result->error) return NULL;

    result->allowedCount = CPU_COUNT(&result->allowed);
    result->passed = result->allowedCount == 1
                  && CPU_ISSET(result->expectedCPU, &result->allowed)
                  && result->runningCPU == result->expectedCPU;
    return NULL;
}
#endif

int VerifyThreadAffinity(void) {
#ifndef __linux__
    puts("AFFINITY_RESULT status=fail reason=linux-only");
    return 1;
#else
    if (!ThreadBindingEnabled()) {
        puts("AFFINITY_RESULT status=fail reason=cpu-binding-not-configured");
        return 1;
    }

    affinityCheckResults = calloc(Threads->count, sizeof(*affinityCheckResults));
    if (!affinityCheckResults) {
        puts("AFFINITY_RESULT status=fail reason=allocation-failed");
        return 1;
    }

    RunWithAllThreads(InspectWorkerAffinity);

    bool passed = true;
    for (int i = 0; i < Threads->count; ++i) {
        AffinityCheckResult *result = &affinityCheckResults[i];
        printf("AFFINITY_THREAD thread=%d expected_cpu=%d running_cpu=%d allowed_count=%d allowed_cpus=",
               i, result->expectedCPU, result->runningCPU, result->allowedCount);
        bool first = true;
        for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
            if (CPU_ISSET(cpu, &result->allowed)) {
                printf("%s%d", first ? "" : ",", cpu);
                first = false;
            }
        if (first) printf("none");
        printf(" status=%s", result->passed ? "pass" : "fail");
        if (result->error) printf(" error=%d", result->error);
        putchar('\n');
        passed &= result->passed;
    }

    printf("AFFINITY_RESULT threads=%d status=%s\n",
           Threads->count, passed ? "pass" : "fail");
    free(affinityCheckResults);
    affinityCheckResults = NULL;
    return passed ? 0 : 1;
#endif
}


// A failed worker launch must fail the run, not masquerade as a scaling result.
static void CheckThreadCall(int error, const char *operation) {
    if (error) {
        fprintf(stderr, "%s failed: %s\n", operation, strerror(error));
        exit(EXIT_FAILURE);
    }
}

// Allocates memory for thread structs
void InitThreads(int count) {

    if (Threads)  free(Threads);
    if (pthreads) free(pthreads);

    Threads  = calloc(count, sizeof(Thread));
    pthreads = calloc(count, sizeof(pthread_t));

    if (!Threads || !pthreads) {
        fprintf(stderr, "Failed to allocate state for %d threads.\n", count);
        exit(EXIT_FAILURE);
    }

    ReadWorkerCPUs(count);

    // Each thread knows its own index and total thread count
    for (int i = 0; i < count; ++i)
        Threads[i].index = i,
        Threads[i].count = count;
}

// Sorts all rootmoves beginning from the given index
void SortRootMoves(Thread *thread, int begin) {
    RootMove *rootMoves = thread->rootMoves;

    for (int i = begin + 1; i < thread->rootMoveCount; ++i) {
        RootMove temp = rootMoves[i];
        int j = i - 1;
        while (j >= 0 && rootMoves[j].score < temp.score) {
            rootMoves[j+1] = rootMoves[j];
            --j;
        }
        rootMoves[j+1] = temp;
    }
}

// Tallies the nodes searched by all threads
uint64_t TotalNodes() {
    uint64_t total = 0;
    for (int i = 0; i < Threads->count; ++i)
        total += Threads[i].pos.nodes;
    return total;
}

// Tallies the tbhits of all threads
uint64_t TotalTBHits() {
    uint64_t total = 0;
    for (int i = 0; i < Threads->count; ++i)
        total += Threads[i].tbhits;
    return total;
}

// Setup threads for a new search
void PrepareSearch(Position *pos, Move searchmoves[]) {

    MoveList legalMoves;
    legalMoves.count = legalMoves.next = 0;
    GenLegalMoves(pos, &legalMoves);

    RootMove rootMoves[256] = { 0 };
    int rootMoveCount = 0;

    // Add legal searchmoves to the root moves by checking if it is in the legalMoves list
    for (Move *move = searchmoves; *move; ++move)
        for (int i = 0; i < legalMoves.count; ++i)
            if (legalMoves.moves[i].move == *move)
                rootMoves[rootMoveCount++].move = *move;

    // If no searchmoves are provided, add all legal moves to the root moves
    if (!rootMoveCount)
        for (int i = 0; i < legalMoves.count; ++i)
            rootMoves[rootMoveCount++].move = legalMoves.moves[i].move;

    for (Thread *t = Threads; t < Threads + Threads->count; ++t) {
        memset(t, 0, offsetof(Thread, pos));
        memcpy(&t->pos, pos, sizeof(Position));
        memcpy(t->rootMoves, rootMoves, sizeof(rootMoves));
        t->rootMoveCount = rootMoveCount;
        for (Depth d = 0; d <= MAX_PLY; ++d)
            (t->ss+SS_OFFSET+d)->ply = d;
        for (Depth d = -7; d < 0; ++d)
            (t->ss+SS_OFFSET+d)->continuation = &t->continuation[0][0][EMPTY][0],
            (t->ss+SS_OFFSET+d)->contCorr = &t->contCorrHistory[EMPTY][0];
    }
}

// Start the main thread running the provided function
void StartMainThread(void *(*func)(void *), Position *pos) {
    CreateWorker(0, func, pos);
    CheckThreadCall(pthread_detach(pthreads[0]), "pthread_detach");
}

static bool helpersActive = false;

// Start helper threads running the provided function
void StartHelpers(void *(*func)(void *)) {
    helpersActive = true;
    for (int i = 1; i < Threads->count; ++i)
        CreateWorker(i, func, &Threads[i]);
}

// Wait for helper threads to finish
void WaitForHelpers() {
    if (!helpersActive) return;
    for (int i = 1; i < Threads->count; ++i)
        CheckThreadCall(pthread_join(pthreads[i], NULL), "pthread_join");
    helpersActive = false;
}

// Reset all data that isn't reset each turn
void ResetThreads() {
    for (int i = 0; i < Threads->count; ++i)
        memset(Threads[i].pawnCache,        0, sizeof(PawnCache)),
        memset(Threads[i].history,          0, sizeof(Threads[i].history)),
        memset(Threads[i].pawnHistory,      0, sizeof(Threads[i].pawnHistory)),
        memset(Threads[i].captureHistory,   0, sizeof(Threads[i].captureHistory)),
        memset(Threads[i].continuation,     0, sizeof(Threads[i].continuation)),
        memset(Threads[i].pawnCorrHistory,  0, sizeof(Threads[i].pawnCorrHistory)),
        memset(Threads[i].minorCorrHistory, 0, sizeof(Threads[i].minorCorrHistory)),
        memset(Threads[i].majorCorrHistory, 0, sizeof(Threads[i].majorCorrHistory)),
        memset(Threads[i].contCorrHistory,  0, sizeof(Threads[i].contCorrHistory));
}

// Run the given function once in each thread
void RunWithAllThreads(void *(*func)(void *)) {
    for (int i = 0; i < Threads->count; ++i)
        CreateWorker(i, func, &Threads[i]);
    for (int i = 0; i < Threads->count; ++i)
        CheckThreadCall(pthread_join(pthreads[i], NULL), "pthread_join");
}

// Thread sleeps until it is woken up
void Wait(atomic_bool *condition) {
    pthread_mutex_lock(&mutex);
    while (!atomic_load(condition))
        pthread_cond_wait(&sleepCondition, &mutex);
    pthread_mutex_unlock(&mutex);
}

// Wakes up a sleeping thread
void Wake() {
    pthread_mutex_lock(&mutex);
    pthread_cond_signal(&sleepCondition);
    pthread_mutex_unlock(&mutex);
}

// Service Under Test — Group G18
//
// The entire experiment depends on ONE design decision, implemented below:
// /healthz and /work contend for the SAME bounded worker pool.
//
// This reproduces the real-world pattern where a health endpoint is served
// by the same thread pool / event loop / CPU quota as production traffic.
// It is the mechanism by which offered load is transmitted into probe
// latency. If you "fix" this by giving /healthz its own goroutine, the
// phenomenon under study disappears and the project has no result.
//
// Env:
//   WORKERS   number of concurrent slots in the pool      (default 4)
//   WORK_MS   CPU-burn duration per /work request in ms   (default 50)
//   PORT      listen port                                 (default 8080)

package main

import (
	"crypto/sha256"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync/atomic"
	"time"
)

var (
	pool        chan struct{}
	workMS      int
	inflight    int64
	servedWork  uint64
	servedHlth  uint64
	rejectedHlth uint64
)

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// burn does real CPU work for d. Cannot be optimised away.
func burn(d time.Duration) {
	deadline := time.Now().Add(d)
	buf := []byte("g18-probe-cascade-study")
	for time.Now().Before(deadline) {
		for i := 0; i < 2000; i++ {
			h := sha256.Sum256(buf)
			buf = h[:]
		}
	}
}

// acquire blocks until a pool slot is free, or the client goes away.
// The blocking IS the experiment: under load the queue builds, and a
// liveness probe waiting here will exceed timeoutSeconds.
func acquire(r *http.Request) bool {
	select {
	case pool <- struct{}{}:
		return true
	case <-r.Context().Done():
		return false
	}
}

func release() { <-pool }

func handleWork(w http.ResponseWriter, r *http.Request) {
	if !acquire(r) {
		http.Error(w, "client cancelled", 499)
		return
	}
	defer release()

	atomic.AddInt64(&inflight, 1)
	defer atomic.AddInt64(&inflight, -1)

	burn(time.Duration(workMS) * time.Millisecond)
	atomic.AddUint64(&servedWork, 1)
	fmt.Fprintln(w, "ok")
}

// handleHealth does almost no work — but it must wait its turn.
func handleHealth(w http.ResponseWriter, r *http.Request) {
	if !acquire(r) {
		// Probe timed out waiting for a slot. This is the false positive.
		atomic.AddUint64(&rejectedHlth, 1)
		return
	}
	defer release()

	atomic.AddUint64(&servedHlth, 1)
	fmt.Fprintln(w, "healthy")
}

// /stats is for the researchers, never for probes. Deliberately does NOT
// touch the pool, so it stays answerable during a cascade.
func handleStats(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintf(w,
		"inflight %d\nqueue_slots_free %d\nwork_served %d\nhealth_served %d\nhealth_abandoned %d\n",
		atomic.LoadInt64(&inflight),
		cap(pool)-len(pool),
		atomic.LoadUint64(&servedWork),
		atomic.LoadUint64(&servedHlth),
		atomic.LoadUint64(&rejectedHlth),
	)
}

func main() {
	workers := envInt("WORKERS", 4)
	workMS = envInt("WORK_MS", 50)
	port := envInt("PORT", 8080)

	pool = make(chan struct{}, workers)

	host, _ := os.Hostname()
	log.Printf("SUT start pod=%s workers=%d work_ms=%d port=%d", host, workers, workMS, port)

	mux := http.NewServeMux()
	mux.HandleFunc("/work", handleWork)
	mux.HandleFunc("/healthz", handleHealth)
	mux.HandleFunc("/stats", handleStats)

	srv := &http.Server{
		Addr:    fmt.Sprintf(":%d", port),
		Handler: mux,
		// No WriteTimeout: we want slow responses, not server-side cutoffs.
	}
	log.Fatal(srv.ListenAndServe())
}

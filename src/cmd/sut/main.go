// Service Under Test (SUT) -- Group G18, Deliverable 02
//
// One binary, two roles (ROLE env):
//
//	ROLE=sut (default)  the replicated web service that is probed
//	ROLE=dep            a tiny downstream dependency used by /work; its
//	                    /admin/stall endpoint creates a shared brown-out
//
// SUT endpoints
//
//	/work          real traffic: [call DEP_URL] + fixed CPU work, inside the worker pool
//	/healthz       BASELINE health check, served from the SAME worker pool
//	               (this is how load turns into probe latency -- the gap)
//	/livez         COMPONENT A: progress-based liveness, outside the pool
//	/livez-naive   comparison arm: isolated "always 200" check (cannot see hangs)
//	/stats         counters for researchers (outside the pool)
//	/admin/hang    fault injection: every worker blocks forever (silent hang)
//	/admin/exit    in-place restart hook used by the governor (Component B)
//
// FIX vs Cutoff-1 code: the old burn() looped until a WALL-CLOCK deadline,
// so a CPU limit could never slow it down and "the cgroup quota binds" was
// not true. burn() now performs a FIXED AMOUNT OF CPU WORK (WORK_UNITS hash
// rounds). Under a 200m limit the same request takes ~5x longer than on a
// free core, exactly as in a real CPU-bound service.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync/atomic"
	"time"

	"g18/internal/progress"
)

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// ---------------------------------------------------------------------
// SUT role
// ---------------------------------------------------------------------

var (
	pool      chan struct{}
	workUnits int
	tracker   *progress.Tracker
	depURL    string
	depClient *http.Client

	servedWork, failedDep, servedHealth, abandonedHealth, abandonedWork uint64
	hung                                                                atomic.Bool
)

// burn performs a fixed amount of CPU work. It cannot be optimised away.
func burn(units int) byte {
	buf := [32]byte{'g', '1', '8'}
	for i := 0; i < units; i++ {
		buf = sha256.Sum256(buf[:])
	}
	return buf[0]
}

// acquire blocks until a pool slot is free or the client gives up.
// Go channels serve blocked senders in FIFO order, so a health check
// waits behind every request that arrived before it.
func acquire(ctx context.Context) bool {
	select {
	case pool <- struct{}{}:
		return true
	case <-ctx.Done():
		return false
	}
}

func release() { <-pool }

func handleWork(w http.ResponseWriter, r *http.Request) {
	tracker.Begin()
	if !acquire(r.Context()) {
		atomic.AddUint64(&abandonedWork, 1)
		tracker.End(false)
		return
	}
	defer release()
	if depURL != "" {
		resp, err := depClient.Get(depURL)
		if err != nil || resp.StatusCode != 200 {
			if resp != nil {
				resp.Body.Close()
			}
			atomic.AddUint64(&failedDep, 1)
			tracker.End(true) // the worker finished processing: that is progress
			http.Error(w, "dependency failed", http.StatusBadGateway)
			return
		}
		resp.Body.Close()
	}
	b := burn(workUnits)
	atomic.AddUint64(&servedWork, 1)
	tracker.End(true)
	fmt.Fprintf(w, "ok %d\n", b)
}

func handleHealthz(w http.ResponseWriter, r *http.Request) {
	if !acquire(r.Context()) {
		atomic.AddUint64(&abandonedHealth, 1) // the false positive, seen from inside
		return
	}
	defer release()
	atomic.AddUint64(&servedHealth, 1)
	fmt.Fprintln(w, "healthy")
}

func handleNaive(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintln(w, "alive")
}

func handleStats(w http.ResponseWriter, r *http.Request) {
	s := tracker.Check()
	_ = json.NewEncoder(w).Encode(map[string]any{
		"pool_busy":        len(pool),
		"pool_size":        cap(pool),
		"pending":          s.Pending,
		"since_progress_s": s.SinceProgress,
		"work_served":      atomic.LoadUint64(&servedWork),
		"dep_failed":       atomic.LoadUint64(&failedDep),
		"work_abandoned":   atomic.LoadUint64(&abandonedWork),
		"health_served":    atomic.LoadUint64(&servedHealth),
		"health_abandoned": atomic.LoadUint64(&abandonedHealth),
		"hung":             hung.Load(),
	})
}

// handleHang makes every worker block forever. Nothing is logged that a
// log-based monitor (SKI) could match: real hangs are silent.
func handleHang(w http.ResponseWriter, r *http.Request) {
	if hung.Swap(true) {
		fmt.Fprintln(w, "already hung")
		return
	}
	for i := 0; i < cap(pool); i++ {
		go func() {
			tracker.Begin()
			pool <- struct{}{}
			select {} // never released, never completes
		}()
	}
	log.Printf("admin: hang injected (silent)")
	fmt.Fprintln(w, "hung")
}

func handleExit(w http.ResponseWriter, r *http.Request) {
	log.Printf("admin: restart requested by %s", r.RemoteAddr)
	fmt.Fprintln(w, "exiting")
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	go func() { time.Sleep(50 * time.Millisecond); os.Exit(3) }()
}

func runSUT() {
	workers := envInt("WORKERS", 4)
	workUnits = envInt("WORK_UNITS", 40000)
	port := envInt("PORT", 8080)
	window := time.Duration(envInt("LIVENESS_WINDOW_MS", 5000)) * time.Millisecond
	depURL = os.Getenv("DEP_URL")
	depClient = &http.Client{Timeout: time.Duration(envInt("DEP_TIMEOUT_MS", 30000)) * time.Millisecond}

	pool = make(chan struct{}, workers)
	tracker = progress.New(window)

	// Self-measure the CPU cost of one request (before any CPU limit kicks in
	// this is only indicative; calibrate.sh measures real capacity).
	t0 := time.Now()
	burn(workUnits)
	host, _ := os.Hostname()
	log.Printf("G18-READY role=sut pod=%s workers=%d work_units=%d (~%.1fms CPU on a free core) window=%v dep=%q port=%d",
		host, workers, workUnits, float64(time.Since(t0).Microseconds())/1000, window, depURL, port)

	mux := http.NewServeMux()
	mux.HandleFunc("/work", handleWork)
	mux.HandleFunc("/healthz", handleHealthz)
	mux.Handle("/livez", tracker.Handler())
	mux.HandleFunc("/livez-naive", handleNaive)
	mux.HandleFunc("/stats", handleStats)
	mux.HandleFunc("/admin/hang", handleHang)
	mux.HandleFunc("/admin/exit", handleExit)

	// NOTE: /livez is outside the worker pool but still needs a Go scheduler
	// thread. Keep GOMAXPROCS above WORKERS (the Go default in a container
	// is the number of host CPUs, e.g. 8 on the HP 348). With GOMAXPROCS=1
	// a saturated process can delay even /livez by seconds (we measured it).
	srv := &http.Server{Addr: fmt.Sprintf(":%d", port), Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	log.Fatal(srv.ListenAndServe())
}

// ---------------------------------------------------------------------
// Dependency role
// ---------------------------------------------------------------------

var stallUntil atomic.Int64 // unix nanos

func handleDep(w http.ResponseWriter, r *http.Request) {
	if until := stallUntil.Load(); until > 0 {
		if d := time.Until(time.Unix(0, until)); d > 0 {
			select {
			case <-time.After(d):
			case <-r.Context().Done():
				return
			}
		}
	}
	fmt.Fprintln(w, "dep ok")
}

func handleStall(w http.ResponseWriter, r *http.Request) {
	sec, _ := strconv.ParseFloat(r.URL.Query().Get("seconds"), 64)
	if sec <= 0 {
		sec = 20
	}
	until := time.Now().Add(time.Duration(sec * float64(time.Second)))
	stallUntil.Store(until.UnixNano())
	log.Printf("admin: dependency stalled for %.1fs (until %s)", sec, until.Format(time.RFC3339Nano))
	fmt.Fprintf(w, "stalled %.1fs\n", sec)
}

func runDep() {
	port := envInt("PORT", 8080)
	log.Printf("G18-READY role=dep port=%d", port)
	mux := http.NewServeMux()
	mux.HandleFunc("/dep", handleDep)
	mux.HandleFunc("/admin/stall", handleStall)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "ok") })
	srv := &http.Server{Addr: fmt.Sprintf(":%d", port), Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	log.Fatal(srv.ListenAndServe())
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds | log.LUTC)
	switch envStr("ROLE", "sut") {
	case "dep":
		runDep()
	default:
		runSUT()
	}
}

// Package progress implements Component A of the G18 proposed method:
// PROGRESS-BASED LIVENESS (PBL).
//
// Idea in one line: a container is "alive" if it is still finishing work,
// not if it can answer a probe quickly.
//
// A poll-based liveness probe (the baseline in Roberts et al.) asks
// "did /healthz answer within timeoutSeconds?". Under overload the answer
// is "no" even though the container is healthy, because the health request
// waits in the same queue as real traffic. That is a false positive.
//
// PBL asks a different question:
//
//		alive  <=>  (nothing is pending)  OR  (some request finished in the last W seconds)
//
//	  - An overloaded container keeps finishing requests -> alive (correct).
//	  - A hung / deadlocked container has pending work but finishes nothing
//	    -> dead after W seconds (correct, so real hangs are still caught).
//	  - An idle container has nothing pending -> alive (correct).
//
// The check itself is served OUTSIDE the worker pool, so it is answered in
// microseconds no matter how long the queue is. Unlike a naive "always 200"
// endpoint it still detects hangs (see the H experiment).
package progress

import (
	"encoding/json"
	"net/http"
	"sync/atomic"
	"time"
)

// Tracker counts pending work and remembers the time of the last progress.
type Tracker struct {
	pending      atomic.Int64
	lastProgress atomic.Int64 // unix nanoseconds
	window       time.Duration
	now          func() time.Time
}

// New returns a tracker with stall window w.
func New(w time.Duration) *Tracker {
	t := &Tracker{window: w, now: time.Now}
	t.lastProgress.Store(t.now().UnixNano())
	return t
}

// NewWithClock is used by tests.
func NewWithClock(w time.Duration, now func() time.Time) *Tracker {
	t := &Tracker{window: w, now: now}
	t.lastProgress.Store(now().UnixNano())
	return t
}

// Begin must be called when a unit of work is admitted (queued or running).
// When the container goes from idle to busy, the progress clock restarts:
// a container that was idle for an hour is not "stalled for an hour".
func (t *Tracker) Begin() {
	if t.pending.Add(1) == 1 {
		t.lastProgress.Store(t.now().UnixNano())
	}
}

// End must be called when a unit of work leaves. completed=true means it
// actually finished (progress); false means it was abandoned (client left).
func (t *Tracker) End(completed bool) {
	if completed {
		t.lastProgress.Store(t.now().UnixNano())
	}
	t.pending.Add(-1)
}

// Snapshot is the state reported by the liveness endpoint.
type Snapshot struct {
	Alive         bool    `json:"alive"`
	Pending       int64   `json:"pending"`
	SinceProgress float64 `json:"since_progress_s"`
	Window        float64 `json:"window_s"`
}

// Check evaluates the progress rule.
func (t *Tracker) Check() Snapshot {
	p := t.pending.Load()
	since := time.Duration(t.now().UnixNano() - t.lastProgress.Load())
	alive := p <= 0 || since < t.window
	return Snapshot{Alive: alive, Pending: p, SinceProgress: since.Seconds(), Window: t.window.Seconds()}
}

// Handler returns 200 when alive and 503 when stalled.
func (t *Tracker) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s := t.Check()
		w.Header().Set("Content-Type", "application/json")
		if !s.Alive {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		_ = json.NewEncoder(w).Encode(s)
	})
}

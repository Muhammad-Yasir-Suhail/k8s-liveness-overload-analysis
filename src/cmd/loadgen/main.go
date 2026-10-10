// Load generator -- Group G18, Deliverable 02
//
// Runs INSIDE the cluster as a Pod and sends traffic to the Kubernetes
// Service, so kube-proxy spreads requests over all ready replicas.
//
// FIX vs Cutoff-1: the old scripts used `kubectl port-forward svc/sut`.
// port-forward picks ONE pod behind the service and tunnels every request
// to it, so the other five replicas got no traffic, and the tunnel died
// whenever that pod restarted. Load redistribution -- the mechanism of the
// whole study -- could not happen.
//
// Modes
//
//	MODE=open    constant arrival rate (default). Clients keep arriving even
//	             when replicas die, like real users. Optional Poisson gaps.
//	             BURST_AT_S / BURST_LEN_S / BURST_RATE add a load spike.
//	MODE=probe   measure latency of PROBE_URLS once per second (calibration)
//	MODE=closed  USERS virtual users, each sends one request every PACE_MS
//	             (like Locust's constant_throughput), used for the Roberts
//	             et al. reproduction.
//
// Output (stdout, collected with `kubectl logs`):
//
//	START <unix_ms>
//	SEC,<second>,<sent>,<ok>,<timeout>,<http5xx>,<other>,<p50_ms>,<p95_ms>,<p99_ms>
//	SUMMARY {json}
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type rec struct {
	start   float64 // seconds since START
	latency float64 // ms
	outcome byte    // 'o' ok, 't' timeout, '5' 5xx, 'e' other error, 'd' dropped
}

func envInt(k string, d int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return d
}
func envF(k string, d float64) float64 {
	if v := os.Getenv(k); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return d
}
func envS(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

var (
	target   string
	client   *http.Client
	mu       sync.Mutex
	records  []rec
	t0       time.Time
	inflight atomic.Int64
)

func fire() {
	st := time.Now()
	r := rec{start: st.Sub(t0).Seconds()}
	resp, err := client.Get(target)
	r.latency = float64(time.Since(st).Microseconds()) / 1000
	switch {
	case err != nil:
		var ne net.Error
		if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
			r.outcome = 't'
		} else {
			r.outcome = 'e'
		}
	default:
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode >= 500 {
			r.outcome = '5'
		} else if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			r.outcome = 'o'
		} else {
			r.outcome = 'e'
		}
	}
	mu.Lock()
	records = append(records, r)
	mu.Unlock()
}

func pct(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return math.NaN()
	}
	i := int(math.Ceil(p/100*float64(len(sorted)))) - 1
	if i < 0 {
		i = 0
	}
	return sorted[i]
}

// runProbe measures the latency of one or more URLs once per second.
// Used by the calibration step to show the load -> health-latency coupling.
// Output: PROBE,<unix_ms>,<url>,<latency_ms>,<status or "timeout"/"error">
func runProbe(dur, timeout time.Duration) {
	urls := strings.Split(envS("PROBE_URLS", "http://sut/healthz"), ",")
	c := &http.Client{Timeout: timeout, Transport: &http.Transport{DisableKeepAlives: true}}
	end := time.Now().Add(dur)
	for time.Now().Before(end) {
		tick := time.Now()
		for _, u := range urls {
			st := time.Now()
			resp, err := c.Get(u)
			lat := float64(time.Since(st).Microseconds()) / 1000
			res := "error"
			if err == nil {
				res = strconv.Itoa(resp.StatusCode)
				resp.Body.Close()
			} else {
				var ne net.Error
				if errors.As(err, &ne) && ne.Timeout() {
					res = "timeout"
				}
			}
			fmt.Printf("PROBE,%d,%s,%.1f,%s\n", st.UnixMilli(), u, lat, res)
		}
		if sl := time.Second - time.Since(tick); sl > 0 {
			time.Sleep(sl)
		}
	}
}

// nz turns NaN into -1 so the summary is always valid JSON.
func nz(f float64) float64 {
	if math.IsNaN(f) {
		return -1
	}
	return f
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	target = envS("TARGET", "http://sut/work")
	mode := envS("MODE", "open")
	rate := envF("RATE", 100)
	dur := time.Duration(envF("DURATION_S", 240) * float64(time.Second))
	timeout := time.Duration(envInt("TIMEOUT_MS", 2000)) * time.Millisecond
	keepAlive := envInt("KEEPALIVE", 0) == 1
	poisson := envInt("POISSON", 1) == 1
	maxInflight := int64(envInt("MAX_INFLIGHT", 20000))
	users := envInt("USERS", 100)
	pace := time.Duration(envInt("PACE_MS", 1000)) * time.Millisecond
	rand.Seed(int64(envInt("SEED", int(time.Now().UnixNano()%1e9))))

	tr := &http.Transport{
		DisableKeepAlives:   !keepAlive,
		MaxIdleConns:        1000,
		MaxIdleConnsPerHost: 1000,
		DialContext:         (&net.Dialer{Timeout: timeout}).DialContext,
	}
	client = &http.Client{Transport: tr, Timeout: timeout}

	// Optional warm wait until the target answers once (max 60 s).
	for i := 0; i < 60; i++ {
		c := &http.Client{Timeout: time.Second, Transport: tr}
		if resp, err := c.Get(target); err == nil {
			resp.Body.Close()
			break
		}
		time.Sleep(time.Second)
	}

	if mode == "probe" {
		runProbe(dur, timeout)
		return
	}

	t0 = time.Now()
	fmt.Printf("START %d\n", t0.UnixMilli())
	log.Printf("loadgen mode=%s target=%s rate=%.1f users=%d dur=%v timeout=%v keepalive=%v",
		mode, target, rate, users, dur, timeout, keepAlive)

	var wg sync.WaitGroup
	end := t0.Add(dur)
	var dropped int64

	if mode == "closed" {
		for u := 0; u < users; u++ {
			wg.Add(1)
			go func(u int) {
				defer wg.Done()
				// spread users over the first pace interval
				if pace > 0 {
					time.Sleep(time.Duration(rand.Int63n(int64(pace))))
				}
				for time.Now().Before(end) {
					st := time.Now()
					fire()
					if sl := pace - time.Since(st); sl > 0 {
						time.Sleep(sl)
					}
				}
			}(u)
		}
	} else {
		next := t0
		burstAt := envF("BURST_AT_S", -1)
		burstLen := envF("BURST_LEN_S", 0)
		burstRate := envF("BURST_RATE", rate)
		for {
			cur := rate
			if el := next.Sub(t0).Seconds(); burstAt >= 0 && el >= burstAt && el < burstAt+burstLen {
				cur = burstRate // load spike (experiment P)
			}
			mean := float64(time.Second) / cur
			if poisson {
				next = next.Add(time.Duration(rand.ExpFloat64() * mean))
			} else {
				next = next.Add(time.Duration(mean))
			}
			if next.After(end) {
				break
			}
			if d := time.Until(next); d > 0 {
				time.Sleep(d)
			}
			if inflight.Load() >= maxInflight {
				atomic.AddInt64(&dropped, 1)
				mu.Lock()
				records = append(records, rec{start: time.Since(t0).Seconds(), outcome: 'd'})
				mu.Unlock()
				continue
			}
			wg.Add(1)
			inflight.Add(1)
			go func() { defer wg.Done(); defer inflight.Add(-1); fire() }()
		}
	}
	wg.Wait()

	// ---- per-second aggregation ----------------------------------------
	secs := int(math.Ceil(dur.Seconds()))
	type agg struct {
		sent, ok, to, s5, other int
		lat                     []float64
	}
	buckets := make([]agg, secs+1)
	var all []float64
	tot := map[byte]int{}
	for _, r := range records {
		s := int(r.start)
		if s > secs {
			s = secs
		}
		b := &buckets[s]
		b.sent++
		tot[r.outcome]++
		switch r.outcome {
		case 'o':
			b.ok++
			b.lat = append(b.lat, r.latency)
			all = append(all, r.latency)
		case 't':
			b.to++
		case '5':
			b.s5++
		default:
			b.other++
		}
	}
	for s, b := range buckets {
		sort.Float64s(b.lat)
		fmt.Printf("SEC,%d,%d,%d,%d,%d,%d,%.1f,%.1f,%.1f\n", s, b.sent, b.ok, b.to, b.s5, b.other,
			pct(b.lat, 50), pct(b.lat, 95), pct(b.lat, 99))
	}
	sort.Float64s(all)
	total := len(records)
	sum := map[string]any{
		"mode": mode, "rate": rate, "users": users, "duration_s": dur.Seconds(),
		"timeout_ms": timeout.Milliseconds(), "total": total, "ok": tot['o'],
		"timeouts": tot['t'], "http5xx": tot['5'], "other_errors": tot['e'], "dropped": tot['d'],
		"failed":       total - tot['o'],
		"success_rate": float64(tot['o']) / math.Max(1, float64(total)),
		"p50_ms":       nz(pct(all, 50)), "p95_ms": nz(pct(all, 95)), "p99_ms": nz(pct(all, 99)),
		"goodput_rps": float64(tot['o']) / dur.Seconds(),
	}
	b, _ := json.Marshal(sum)
	fmt.Printf("SUMMARY %s\n", b)
	log.Printf("done: %s", b)
}

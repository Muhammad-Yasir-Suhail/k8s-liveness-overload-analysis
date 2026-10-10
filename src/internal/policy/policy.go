// Package policy implements Component B of the G18 proposed method:
// the CLUSTER-AWARE RESTART GOVERNOR (CRG).
//
// Roberts et al. note that their signal-based monitor "does not aggregate
// status across Pods before restarting". Neither does the kubelet: every
// container is judged alone. When the cause of unhealthiness is SHARED
// (overload, a slow dependency), every replica fails at the same moment
// and the kubelet restarts all of them -- removing capacity exactly when
// it is needed most.
//
// The governor looks at all replicas together and applies three rules:
//
//  1. Correlation guard: if at least CorrFrac of the replicas are unhealthy
//     at the same time (and at least 2), the cause is probably shared, so a
//     restart will not fix it. Restarts are HELD for up to Hold seconds.
//  2. Restart budget: at most Budget replicas may be "in restart" at once
//     (like maxUnavailable for a rolling update).
//  3. Futility back-off: if a pod the governor restarted becomes unhealthy
//     again within FutileWindow, the restart did not help, so the hold time
//     is doubled (up to HoldMax).
//
// Independent failures (one hung pod) are still restarted immediately,
// because they never trip the correlation guard.
//
// This file is pure logic (no Kubernetes calls) so it can be unit tested
// and re-used 1:1 by the simulator (sim/probesim.py mirrors it).
package policy

import (
	"math"
	"sort"
	"time"
)

// Config holds the governor parameters.
type Config struct {
	Threshold    int           // consecutive failed checks before a pod is a suspect
	BudgetFrac   float64       // max fraction of replicas restarting at once
	CorrFrac     float64       // >= this fraction unhealthy => correlated (set >1 to disable)
	Hold         time.Duration // initial hold time when correlated
	HoldMax      time.Duration // upper bound for the hold time
	FutileWindow time.Duration // relapse window that marks a restart as futile
	Grace        time.Duration // ignore pods younger than this (startup)
	InFlightMax  time.Duration // forget an in-flight restart after this long
}

// Default returns the parameters used in the report.
func Default() Config {
	return Config{
		Threshold:    1,
		BudgetFrac:   0.25,
		CorrFrac:     0.5,
		Hold:         60 * time.Second,
		HoldMax:      300 * time.Second,
		FutileWindow: 30 * time.Second,
		Grace:        10 * time.Second,
		InFlightMax:  120 * time.Second,
	}
}

// Pod is what the governor knows about one replica in one tick.
type Pod struct {
	Name        string
	Running     bool          // container is running
	Age         time.Duration // time since container (re)start
	ConsecFails int           // consecutive failed checks
	Healthy     bool          // last check succeeded
}

// Decision is the output of one tick.
type Decision struct {
	Restart    []string
	Suppressed bool
	Suspects   int
	Eligible   int
	Reason     string
}

// Governor keeps state across ticks.
type Governor struct {
	cfg       Config
	holdStart time.Time
	holdLen   time.Duration
	inFlight  map[string]time.Time // pod -> when restart was issued
	recovered map[string]time.Time // pod -> when it became healthy after our restart
}

// New creates a governor.
func New(cfg Config) *Governor {
	return &Governor{cfg: cfg, holdLen: cfg.Hold,
		inFlight: map[string]time.Time{}, recovered: map[string]time.Time{}}
}

// HoldLen exposes the current hold length (for logs/tests).
func (g *Governor) HoldLen() time.Duration { return g.holdLen }

// Decide runs one tick of the policy.
func (g *Governor) Decide(now time.Time, pods []Pod) Decision {
	n := len(pods)
	d := Decision{}
	if n == 0 {
		return d
	}

	// --- bookkeeping for in-flight and recovered restarts -------------
	for _, p := range pods {
		if t, ok := g.inFlight[p.Name]; ok {
			if p.Running && p.Healthy && p.Age >= time.Second {
				delete(g.inFlight, p.Name)
				g.recovered[p.Name] = now
			} else if now.Sub(t) > g.cfg.InFlightMax {
				delete(g.inFlight, p.Name)
			}
		}
	}

	var suspects []Pod
	for _, p := range pods {
		if !p.Running || p.Age < g.cfg.Grace {
			continue
		}
		d.Eligible++
		if p.ConsecFails >= g.cfg.Threshold {
			suspects = append(suspects, p)
			// futility: relapsed soon after our restart
			if t, ok := g.recovered[p.Name]; ok {
				if now.Sub(t) <= g.cfg.FutileWindow {
					g.holdLen = minDur(2*g.holdLen, g.cfg.HoldMax)
				}
				delete(g.recovered, p.Name)
			}
		}
	}
	d.Suspects = len(suspects)
	if len(suspects) == 0 {
		g.holdStart = time.Time{}
		d.Reason = "all healthy"
		return d
	}

	// --- rule 1: correlation guard -----------------------------------
	frac := float64(len(suspects)) / float64(n)
	if frac >= g.cfg.CorrFrac && len(suspects) >= 2 {
		if g.holdStart.IsZero() {
			g.holdStart = now
		}
		if now.Sub(g.holdStart) < g.holdLen {
			d.Suppressed = true
			d.Reason = "correlated unhealthiness: restart held"
			return d
		}
		// hold expired: fall through, restarts still limited by budget
		d.Reason = "hold expired: budgeted restart"
	} else {
		g.holdStart = time.Time{}
		d.Reason = "independent failure: budgeted restart"
	}

	// --- rule 2: restart budget --------------------------------------
	budget := int(math.Floor(g.cfg.BudgetFrac * float64(n)))
	if budget < 1 {
		budget = 1
	}
	budget -= len(g.inFlight)
	if budget <= 0 {
		d.Reason = "budget exhausted"
		return d
	}
	sort.SliceStable(suspects, func(i, j int) bool {
		return suspects[i].ConsecFails > suspects[j].ConsecFails
	})
	for _, p := range suspects {
		if budget == 0 {
			break
		}
		if _, busy := g.inFlight[p.Name]; busy {
			continue
		}
		d.Restart = append(d.Restart, p.Name)
		g.inFlight[p.Name] = now
		budget--
	}
	if len(d.Restart) > 0 && d.Reason == "hold expired: budgeted restart" {
		// a new episode starts after this probe restart
		g.holdStart = now
	}
	return d
}

func minDur(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

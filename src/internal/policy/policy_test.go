package policy

import (
	"fmt"
	"testing"
	"time"
)

func pods(n, bad int) []Pod {
	ps := make([]Pod, n)
	for i := range ps {
		ps[i] = Pod{Name: fmt.Sprintf("p%d", i), Running: true, Age: time.Minute, Healthy: true}
		if i < bad {
			ps[i].ConsecFails = 3
			ps[i].Healthy = false
		}
	}
	return ps
}

func TestSingleFailureIsRestarted(t *testing.T) {
	g := New(Default())
	d := g.Decide(time.Unix(0, 0), pods(6, 1))
	if len(d.Restart) != 1 || d.Suppressed {
		t.Fatalf("one hung pod must be restarted at once: %+v", d)
	}
}

func TestCorrelatedFailureIsHeld(t *testing.T) {
	g := New(Default())
	t0 := time.Unix(0, 0)
	for s := 0; s < 59; s++ {
		d := g.Decide(t0.Add(time.Duration(s)*time.Second), pods(6, 6))
		if !d.Suppressed || len(d.Restart) != 0 {
			t.Fatalf("t=%ds: correlated failure must be held: %+v", s, d)
		}
	}
	d := g.Decide(t0.Add(61*time.Second), pods(6, 6))
	if d.Suppressed || len(d.Restart) != 1 {
		t.Fatalf("after hold, exactly budget (1) restart expected: %+v", d)
	}
}

func TestBudgetLimitsConcurrentRestarts(t *testing.T) {
	cfg := Default()
	cfg.CorrFrac = 2 // disable guard
	g := New(cfg)
	d := g.Decide(time.Unix(0, 0), pods(8, 4))
	if len(d.Restart) != 2 { // floor(0.25*8)=2
		t.Fatalf("budget 2 expected, got %d", len(d.Restart))
	}
	d = g.Decide(time.Unix(1, 0), pods(8, 4))
	if len(d.Restart) != 0 {
		t.Fatalf("budget already used by in-flight restarts: %+v", d)
	}
}

func TestFutileRestartDoublesHold(t *testing.T) {
	g := New(Default())
	t0 := time.Unix(0, 0)
	g.Decide(t0, pods(6, 6))                          // hold starts
	d := g.Decide(t0.Add(61*time.Second), pods(6, 6)) // budgeted restart of p0
	if len(d.Restart) != 1 {
		t.Fatalf("expected restart: %+v", d)
	}
	ok := pods(6, 6)
	ok[0].ConsecFails, ok[0].Healthy = 0, true
	ok[0].Age = 2 * time.Second
	g.Decide(t0.Add(70*time.Second), ok) // p0 recovered
	bad := pods(6, 6)
	g.Decide(t0.Add(80*time.Second), bad) // p0 relapsed within 30s
	if g.HoldLen() != 120*time.Second {
		t.Fatalf("hold should double to 120s, got %v", g.HoldLen())
	}
}

func TestYoungPodsIgnored(t *testing.T) {
	g := New(Default())
	ps := pods(6, 1)
	ps[0].Age = 3 * time.Second
	if d := g.Decide(time.Unix(0, 0), ps); len(d.Restart) != 0 {
		t.Fatalf("pods inside startup grace must not be restarted: %+v", d)
	}
}

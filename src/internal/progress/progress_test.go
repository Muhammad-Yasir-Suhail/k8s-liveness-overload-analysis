package progress

import (
	"testing"
	"time"
)

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time      { return c.t }
func (c *fakeClock) adv(d time.Duration) { c.t = c.t.Add(d) }

func TestIdleIsAlive(t *testing.T) {
	c := &fakeClock{t: time.Unix(1000, 0)}
	tr := NewWithClock(5*time.Second, c.now)
	c.adv(time.Hour)
	if !tr.Check().Alive {
		t.Fatal("idle container must be alive")
	}
}

func TestIdleThenBusyRestartsClock(t *testing.T) {
	c := &fakeClock{t: time.Unix(1000, 0)}
	tr := NewWithClock(5*time.Second, c.now)
	c.adv(time.Hour)
	tr.Begin()
	c.adv(2 * time.Second)
	if !tr.Check().Alive {
		t.Fatal("work admitted 2s ago, window 5s: must be alive")
	}
}

func TestOverloadedButProgressingIsAlive(t *testing.T) {
	c := &fakeClock{t: time.Unix(1000, 0)}
	tr := NewWithClock(5*time.Second, c.now)
	for i := 0; i < 100; i++ { // long queue
		tr.Begin()
	}
	for i := 0; i < 60; i++ { // one completion every 200ms for 12s
		c.adv(200 * time.Millisecond)
		tr.End(true)
		tr.Begin()
		if !tr.Check().Alive {
			t.Fatalf("progressing container declared dead at step %d", i)
		}
	}
}

func TestHangIsDetected(t *testing.T) {
	c := &fakeClock{t: time.Unix(1000, 0)}
	tr := NewWithClock(5*time.Second, c.now)
	tr.Begin()
	tr.End(true)
	tr.Begin() // stuck forever
	c.adv(4 * time.Second)
	if !tr.Check().Alive {
		t.Fatal("4s < window: still alive")
	}
	c.adv(2 * time.Second)
	if tr.Check().Alive {
		t.Fatal("6s without progress while work is pending: must be dead")
	}
}

func TestAbandonedWorkIsNotProgress(t *testing.T) {
	c := &fakeClock{t: time.Unix(1000, 0)}
	tr := NewWithClock(5*time.Second, c.now)
	tr.Begin() // stuck
	for i := 0; i < 10; i++ {
		c.adv(1 * time.Second)
		tr.Begin()
		tr.End(false) // clients giving up is NOT progress
	}
	if tr.Check().Alive {
		t.Fatal("abandoned requests must not count as progress")
	}
}

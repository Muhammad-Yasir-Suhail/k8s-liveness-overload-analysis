// Cluster-aware Restart Governor -- Group G18, Deliverable 02 (Component B)
//
// Replaces the kubelet's per-container liveness decision with a decision
// made over ALL replicas of a Deployment (see internal/policy).
//
// Every PERIOD it lists the pods, checks each one (HTTP GET PROBE_PATH,
// TIMEOUT), feeds the results to the policy, and restarts the pods the
// policy selects. A restart is done IN PLACE (GET /admin/exit makes the
// process exit; the kubelet restarts the container with its normal
// back-off), so it costs exactly what a kubelet liveness restart costs.
// If the hook does not answer, the pod is deleted (ReplicaSet recreates it).
//
// The workload Deployment must NOT have its own livenessProbe when the
// governor is in charge -- otherwise two controllers would fight.
package main

import (
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"sync"
	"time"

	"g18/internal/kube"
	"g18/internal/policy"
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
func envI(k string, d int) int {
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

type track struct {
	containerID string
	fails       int
	healthy     bool
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds | log.LUTC)
	kc, err := kube.InCluster()
	if err != nil {
		log.Fatal(err)
	}
	ns := env("NAMESPACE", env("POD_NAMESPACE", "default"))
	sel := env("SELECTOR", "app=sut")
	cname := env("CONTAINER", "sut")
	path := env("PROBE_PATH", "/livez")
	port := envI("PROBE_PORT", 8080)
	period := time.Duration(envI("PERIOD_MS", 1000)) * time.Millisecond
	timeout := time.Duration(envI("TIMEOUT_MS", 1000)) * time.Millisecond

	cfg := policy.Default()
	cfg.Threshold = envI("THRESHOLD", cfg.Threshold)
	cfg.BudgetFrac = envF("BUDGET_FRAC", cfg.BudgetFrac)
	cfg.CorrFrac = envF("CORR_FRAC", cfg.CorrFrac)
	cfg.Hold = time.Duration(envF("HOLD_S", cfg.Hold.Seconds()) * float64(time.Second))
	cfg.HoldMax = time.Duration(envF("HOLD_MAX_S", cfg.HoldMax.Seconds()) * float64(time.Second))
	cfg.FutileWindow = time.Duration(envF("FUTILE_S", cfg.FutileWindow.Seconds()) * float64(time.Second))
	cfg.Grace = time.Duration(envF("GRACE_S", cfg.Grace.Seconds()) * float64(time.Second))
	gov := policy.New(cfg)

	log.Printf("GOV start ns=%s sel=%s path=%s period=%v timeout=%v cfg=%+v", ns, sel, path, period, timeout, cfg)

	probeClient := &http.Client{Timeout: timeout, Transport: &http.Transport{DisableKeepAlives: true}}
	exitClient := &http.Client{Timeout: time.Second}
	tracks := map[string]*track{}
	wasHeld := false

	tick := time.NewTicker(period)
	for now := range tick.C {
		var pl kube.PodList
		q := "/api/v1/namespaces/" + ns + "/pods?labelSelector=" + url.QueryEscape(sel)
		if err := kc.Do("GET", q, "", nil, &pl); err != nil {
			log.Printf("GOV-ERROR list: %v", err)
			continue
		}

		type item struct {
			p   kube.Pod
			cs  *kube.ContainerStatus
			ok  bool
			age time.Duration
		}
		var items []*item
		for i := range pl.Items {
			p := pl.Items[i]
			if p.Metadata.DeletionTimestamp != nil {
				continue
			}
			it := &item{p: p, cs: p.Container(cname)}
			items = append(items, it)
		}

		// probe all running pods in parallel
		var wg sync.WaitGroup
		for _, it := range items {
			if it.cs == nil || it.cs.State.Running == nil || it.p.Status.PodIP == "" {
				continue
			}
			it.age = now.Sub(it.cs.State.Running.StartedAt)
			wg.Add(1)
			go func(it *item) {
				defer wg.Done()
				resp, err := probeClient.Get(fmt.Sprintf("http://%s:%d%s", it.p.Status.PodIP, port, path))
				if err == nil {
					it.ok = resp.StatusCode >= 200 && resp.StatusCode < 400
					resp.Body.Close()
				}
			}(it)
		}
		wg.Wait()

		var view []policy.Pod
		byName := map[string]*item{}
		for _, it := range items {
			name := it.p.Metadata.Name
			byName[name] = it
			running := it.cs != nil && it.cs.State.Running != nil
			tr := tracks[name]
			if tr == nil {
				tr = &track{}
				tracks[name] = tr
			}
			if it.cs != nil && tr.containerID != it.cs.ContainerID {
				*tr = track{containerID: it.cs.ContainerID}
			}
			if running {
				if it.ok {
					tr.fails, tr.healthy = 0, true
				} else {
					tr.fails++
					tr.healthy = false
				}
			}
			view = append(view, policy.Pod{Name: name, Running: running, Age: it.age,
				ConsecFails: tr.fails, Healthy: tr.healthy})
		}

		d := gov.Decide(now, view)
		log.Printf("GOV-TICK eligible=%d suspects=%d held=%v restart=%v hold=%v reason=%q",
			d.Eligible, d.Suspects, d.Suppressed, d.Restart, gov.HoldLen(), d.Reason)
		if d.Suppressed && !wasHeld {
			log.Printf("GOV-HOLD start suspects=%d/%d", d.Suspects, len(view))
			if len(items) > 0 {
				kc.Event(ns, items[0].p.Metadata.Name, items[0].p.Metadata.UID, "G18RestartHeld",
					fmt.Sprintf("%d/%d replicas unhealthy together: restart held", d.Suspects, len(view)))
			}
		}
		wasHeld = d.Suppressed

		for _, name := range d.Restart {
			it := byName[name]
			ip := it.p.Status.PodIP
			log.Printf("GOV-RESTART pod=%s fails=%d reason=%q", name, tracks[name].fails, d.Reason)
			kc.Event(ns, name, it.p.Metadata.UID, "G18GovernorRestart", d.Reason)
			resp, err := exitClient.Get(fmt.Sprintf("http://%s:%d/admin/exit", ip, port))
			if err == nil {
				resp.Body.Close()
				continue
			}
			log.Printf("GOV-RESTART exit hook failed (%v): deleting pod %s", err, name)
			_ = kc.Do("DELETE", "/api/v1/namespaces/"+ns+"/pods/"+name, "",
				map[string]any{"gracePeriodSeconds": 2}, nil)
		}
	}
}

// SKI emulation sidecar -- Group G18, Deliverable 02 (baseline reproduction)
//
// Roberts et al. implement Signal-based Container Monitoring for Kubernetes
// (SKI) as a sidecar that FOLLOWS THE MAIN CONTAINER'S LOGS through the
// Kubernetes API and, when a log line matches a pattern, tells a MODIFIED
// KUBELET (over a UNIX socket) to mark the container Ready or to restart it.
// Their code and modified kubelet are not public.
//
// This sidecar reproduces the same signal path with STOCK Kubernetes:
//
//	paper (modified kubelet)              this emulation (stock K8s 1.27)
//	-----------------------------------   ---------------------------------------
//	follow logs via API server            follow logs via API server (same)
//	match substring                       match substring (same)
//	kubelet queues restart                SIGKILL main process via shared PID
//	                                      namespace -> kubelet restarts container
//	2 s termination grace (kubelet min)   KILL_DELAY_MS=2000 (same delay)
//	kubelet marks container Ready         sidecar sets a Pod readinessGate
//	                                      condition (g18.io/ski-ready)
//
// Env: POD_NAME, POD_NAMESPACE (downward API), TARGET_CONTAINER,
// FAIL_PATTERN, READY_PATTERN, READY_FALLBACK_ADDR, READY_FALLBACK_AFTER_MS,
// GATE, KILL_MATCH, KILL_DELAY_MS.
package main

import (
	"bufio"
	"log"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"g18/internal/kube"
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
func envMS(k string, d int) time.Duration {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return time.Duration(n) * time.Millisecond
		}
	}
	return time.Duration(d) * time.Millisecond
}

var (
	kc        *kube.Client
	ns, pod   string
	gate      string
	gateMu    sync.Mutex
	gateState = "unknown"
)

func setGate(ready bool, why string) {
	gateMu.Lock()
	defer gateMu.Unlock()
	st := "False"
	if ready {
		st = "True"
	}
	if gateState == st {
		return
	}
	patch := map[string]any{"status": map[string]any{"conditions": []map[string]any{{
		"type": gate, "status": st, "reason": "SKI", "message": why,
		"lastTransitionTime": time.Now().UTC().Format(time.RFC3339),
	}}}}
	err := kc.Do("PATCH", "/api/v1/namespaces/"+ns+"/pods/"+pod+"/status",
		"application/strategic-merge-patch+json", patch, nil)
	if err != nil {
		log.Printf("SKI-ERROR gate patch: %v", err)
		return
	}
	gateState = st
	log.Printf("SKI-GATE %s (%s)", st, why)
}

// killTarget finds the main container's process (its cmdline contains
// KILL_MATCH) in the shared PID namespace and SIGKILLs it.
func killTarget(match string) int {
	self := os.Getpid()
	n := 0
	dirs, _ := filepath.Glob("/proc/[0-9]*")
	for _, d := range dirs {
		pid, err := strconv.Atoi(filepath.Base(d))
		if err != nil || pid == self {
			continue
		}
		b, err := os.ReadFile(d + "/cmdline")
		if err != nil {
			continue
		}
		cmd := strings.ReplaceAll(string(b), "\x00", " ")
		if strings.Contains(cmd, match) && !strings.Contains(cmd, "/ski") {
			if err := syscall.Kill(pid, syscall.SIGKILL); err == nil {
				log.Printf("SKI-KILL pid=%d cmd=%q", pid, cmd)
				n++
			} else {
				log.Printf("SKI-ERROR kill pid=%d: %v", pid, err)
			}
		}
	}
	return n
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds | log.LUTC)
	var err error
	kc, err = kube.InCluster()
	if err != nil {
		log.Fatal(err)
	}
	ns, pod = env("POD_NAMESPACE", "default"), env("POD_NAME", "")
	target := env("TARGET_CONTAINER", "catalogue")
	failPat := env("FAIL_PATTERN", "G18-FAILURE")
	readyPat := env("READY_PATTERN", "transport=HTTP")
	fbAddr := env("READY_FALLBACK_ADDR", "127.0.0.1:80")
	fbAfter := envMS("READY_FALLBACK_AFTER_MS", 5000)
	gate = env("GATE", "g18.io/ski-ready")
	killMatch := env("KILL_MATCH", "G18WRAP")
	killDelay := envMS("KILL_DELAY_MS", 2000)
	log.Printf("SKI start pod=%s/%s target=%s fail=%q ready=%q", ns, pod, target, failPat, readyPat)

	lastID := ""
	for {
		var p kube.Pod
		if err := kc.Do("GET", "/api/v1/namespaces/"+ns+"/pods/"+pod, "", nil, &p); err != nil {
			log.Printf("SKI-ERROR get pod: %v", err)
			time.Sleep(500 * time.Millisecond)
			continue
		}
		cs := p.Container(target)
		if cs == nil || cs.State.Running == nil {
			setGate(false, "container not running")
			time.Sleep(200 * time.Millisecond)
			continue
		}
		since := ""
		if cs.ContainerID == lastID {
			// the stream dropped but the same container is still running:
			// re-follow only the newest lines so nothing is matched twice
			since = "&sinceSeconds=1"
		}
		lastID = cs.ContainerID
		started := cs.State.Running.StartedAt
		log.Printf("SKI-FOLLOW container=%s restarts=%d started=%s", target, cs.RestartCount, started.Format(time.RFC3339))

		var readySeen sync.Once
		markReady := func(why string) { readySeen.Do(func() { setGate(true, why) }) }

		// Fallback: if the ready pattern never appears, accept a TCP connect.
		done := make(chan struct{})
		go func() {
			wait := fbAfter - time.Since(started)
			if wait > 0 {
				select {
				case <-time.After(wait):
				case <-done:
					return
				}
			}
			for {
				select {
				case <-done:
					return
				default:
				}
				if c, err := net.DialTimeout("tcp", fbAddr, 500*time.Millisecond); err == nil {
					c.Close()
					markReady("fallback tcp connect")
					return
				}
				time.Sleep(500 * time.Millisecond)
			}
		}()

		rc, err := kc.OpenStream("/api/v1/namespaces/" + ns + "/pods/" + pod + "/log?container=" + target + "&follow=true" + since)
		if err != nil {
			log.Printf("SKI-ERROR log stream: %v", err)
			close(done)
			time.Sleep(500 * time.Millisecond)
			lastID = ""
			continue
		}
		sc := bufio.NewScanner(rc)
		sc.Buffer(make([]byte, 64*1024), 1024*1024)
		for sc.Scan() {
			line := sc.Text()
			if strings.Contains(line, readyPat) {
				markReady("ready log line")
			}
			if strings.Contains(line, failPat) {
				log.Printf("SKI-DETECT failure signal: %q", line)
				setGate(false, "failure signal")
				time.Sleep(killDelay)
				if killTarget(killMatch) == 0 {
					log.Printf("SKI-ERROR no process matched %q", killMatch)
				}
			}
		}
		rc.Close()
		close(done)
		// No gate change here: if the container really died, the next
		// iteration sees "not running" and clears the gate.
		time.Sleep(100 * time.Millisecond)
	}
}

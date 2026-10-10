// Package kube is a tiny in-cluster Kubernetes REST client that uses only
// the Go standard library. It avoids client-go so the image builds in
// seconds on a laptop and has no external dependencies.
package kube

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

const saDir = "/var/run/secrets/kubernetes.io/serviceaccount"

// Client talks to the API server with the pod's service-account token.
type Client struct {
	Base   string
	token  string
	HTTP   *http.Client // short requests
	Stream *http.Client // long-lived (log follow)
}

// InCluster builds a client from the standard service-account mount.
func InCluster() (*Client, error) {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" {
		return nil, fmt.Errorf("not running in a cluster")
	}
	tok, err := os.ReadFile(saDir + "/token")
	if err != nil {
		return nil, err
	}
	ca, err := os.ReadFile(saDir + "/ca.crt")
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	tr := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}
	return &Client{
		Base:   "https://" + host + ":" + port,
		token:  string(bytes.TrimSpace(tok)),
		HTTP:   &http.Client{Transport: tr, Timeout: 5 * time.Second},
		Stream: &http.Client{Transport: tr},
	}, nil
}

// Do performs a request and decodes JSON into out (if out != nil).
func (c *Client) Do(method, path, contentType string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.Base+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if contentType == "" {
		contentType = "application/json"
	}
	req.Header.Set("Content-Type", contentType)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%s %s: %d %s", method, path, resp.StatusCode, string(b))
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

// OpenStream opens a streaming GET (used for follow=true logs).
func (c *Client) OpenStream(path string) (io.ReadCloser, error) {
	req, err := http.NewRequest("GET", c.Base+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.Stream.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return nil, fmt.Errorf("stream %s: %d %s", path, resp.StatusCode, string(b))
	}
	return resp.Body, nil
}

// ---- minimal object types -------------------------------------------

type ContainerState struct {
	Running *struct {
		StartedAt time.Time `json:"startedAt"`
	} `json:"running,omitempty"`
	Terminated *struct {
		Reason   string    `json:"reason"`
		ExitCode int       `json:"exitCode"`
		Finished time.Time `json:"finishedAt"`
	} `json:"terminated,omitempty"`
	Waiting *struct {
		Reason string `json:"reason"`
	} `json:"waiting,omitempty"`
}

type ContainerStatus struct {
	Name         string         `json:"name"`
	Ready        bool           `json:"ready"`
	RestartCount int            `json:"restartCount"`
	State        ContainerState `json:"state"`
	ContainerID  string         `json:"containerID"`
}

type Pod struct {
	Metadata struct {
		Name              string     `json:"name"`
		Namespace         string     `json:"namespace"`
		UID               string     `json:"uid"`
		CreationTimestamp time.Time  `json:"creationTimestamp"`
		DeletionTimestamp *time.Time `json:"deletionTimestamp,omitempty"`
	} `json:"metadata"`
	Status struct {
		Phase             string            `json:"phase"`
		PodIP             string            `json:"podIP"`
		ContainerStatuses []ContainerStatus `json:"containerStatuses"`
	} `json:"status"`
}

type PodList struct {
	Items []Pod `json:"items"`
}

// Container finds a container status by name.
func (p *Pod) Container(name string) *ContainerStatus {
	for i := range p.Status.ContainerStatuses {
		if p.Status.ContainerStatuses[i].Name == name {
			return &p.Status.ContainerStatuses[i]
		}
	}
	return nil
}

// Event creates a core/v1 Event so actions show up in `kubectl get events`.
func (c *Client) Event(ns, podName, podUID, reason, msg string) {
	now := time.Now().UTC().Format(time.RFC3339)
	ev := map[string]any{
		"metadata": map[string]any{"generateName": "g18-", "namespace": ns},
		"involvedObject": map[string]any{
			"kind": "Pod", "namespace": ns, "name": podName, "uid": podUID,
		},
		"reason": reason, "message": msg, "type": "Normal",
		"firstTimestamp": now, "lastTimestamp": now, "count": 1,
		"source": map[string]any{"component": "g18-governor"},
	}
	_ = c.Do("POST", "/api/v1/namespaces/"+ns+"/events", "", ev, nil)
}

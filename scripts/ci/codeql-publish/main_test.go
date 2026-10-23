package main

import (
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRejectsNonMainMetadata(t *testing.T) {
	valid := map[string]string{"CI_REPO": repository, "CI_PIPELINE_EVENT": "push", "CI_COMMIT_REF": "refs/heads/main", "CI_COMMIT_BRANCH": "main", "CI_COMMIT_SHA": strings.Repeat("a", 40), "PLUGIN_LANGUAGE": "python", "PLUGIN_TOKEN": "test-token"}
	if _, err := readConfig(func(k string) string { return valid[k] }, t.TempDir()); err != nil {
		t.Fatal(err)
	}
	for key, value := range map[string]string{"CI_REPO": "other/repo", "CI_PIPELINE_EVENT": "pull_request", "CI_COMMIT_REF": "refs/heads/topic", "CI_COMMIT_BRANCH": "topic", "CI_COMMIT_SHA": "HEAD", "PLUGIN_LANGUAGE": "../python", "PLUGIN_TOKEN": ""} {
		t.Run(key, func(t *testing.T) {
			env := map[string]string{}
			for k, v := range valid {
				env[k] = v
			}
			env[key] = value
			if _, err := readConfig(func(k string) string { return env[k] }, t.TempDir()); err == nil {
				t.Fatal("unsafe metadata accepted")
			}
		})
	}
}

func TestUploadRequiresSuccessfulProcessing(t *testing.T) {
	for _, state := range []string{"complete", "failed", "unknown", "denied"} {
		t.Run(state, func(t *testing.T) {
			root := t.TempDir()
			dir := filepath.Join(root, "artifacts", "codeql", "python")
			if err := os.MkdirAll(dir, 0o755); err != nil {
				t.Fatal(err)
			}
			report := `{"version":"2.1.0","runs":[{"results":[{"ruleId":"preserved-finding"}]}]}`
			if err := os.WriteFile(filepath.Join(dir, "results.sarif"), []byte(report), 0o600); err != nil {
				t.Fatal(err)
			}
			polls := 0
			uploads := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer test-token" {
					t.Error("missing authentication")
				}
				if !strings.HasPrefix(r.URL.Path, "/repos/"+repository+"/code-scanning/sarifs") {
					t.Error("wrong repository")
				}
				if state == "denied" {
					w.WriteHeader(403)
					return
				}
				if r.Method == "POST" {
					uploads++
					var body map[string]string
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body["commit_sha"] != strings.Repeat("a", 40) || body["ref"] != "refs/heads/main" {
						t.Error("wrong immutable revision")
					}
					data, err := base64.StdEncoding.DecodeString(body["sarif"])
					if err != nil {
						t.Fatal(err)
					}
					zip, err := gzip.NewReader(strings.NewReader(string(data)))
					if err != nil {
						t.Fatal(err)
					}
					raw, err := io.ReadAll(zip)
					if err != nil || string(raw) != report {
						t.Error("SARIF findings changed")
					}
					zip.Close()
					w.WriteHeader(202)
					io.WriteString(w, `{"id":"receipt-1"}`)
					return
				}
				polls++
				status := state
				if polls == 1 {
					status = "pending"
				}
				json.NewEncoder(w).Encode(map[string]string{"processing_status": status})
			}))
			defer server.Close()
			c := config{strings.Repeat("a", 40), "python", "test-token", root}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			err := publish(ctx, c, server.Client(), server.URL, time.Millisecond)
			if (err == nil) != (state == "complete") {
				t.Fatalf("state=%s err=%v", state, err)
			}
			if state == "complete" && (uploads != 1 || polls != 2) {
				t.Fatal("upload receipt was not checked")
			}
		})
	}
}

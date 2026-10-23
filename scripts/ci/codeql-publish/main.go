// A fixed-purpose Woodpecker plugin. Never executes repository code or a shell.
package main

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"time"
)

const repository = "Perlitten/project-brain-public"
const apiOrigin = "https://api.github.com"

var revisionPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

type config struct{ revision, language, token, workspace string }

func readConfig(get func(string) string, workspace string) (config, error) {
	c := config{get("CI_COMMIT_SHA"), get("PLUGIN_LANGUAGE"), get("PLUGIN_TOKEN"), workspace}
	if get("CI_REPO") != repository || get("CI_PIPELINE_EVENT") != "push" || get("CI_COMMIT_REF") != "refs/heads/main" || get("CI_COMMIT_BRANCH") != "main" {
		return c, errors.New("publication requires this repository's main push")
	}
	if !revisionPattern.MatchString(c.revision) || (c.language != "python" && c.language != "javascript-typescript") || c.token == "" {
		return c, errors.New("invalid revision, language or missing publication token")
	}
	return c, nil
}

func request(ctx context.Context, client *http.Client, method, endpoint, token string, body []byte, result any) error {
	req, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(body))
	if err != nil {
		return errors.New("invalid GitHub request")
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "brain-woodpecker-codeql-publish")
	response, err := client.Do(req)
	if err != nil {
		return errors.New("GitHub publication request failed")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("GitHub publication returned HTTP %d", response.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(result)
}

func publish(ctx context.Context, c config, client *http.Client, origin string, pollInterval time.Duration) error {
	path := filepath.Join(c.workspace, "artifacts", "codeql", c.language, "results.sarif")
	file, err := os.Open(path)
	if err != nil {
		return errors.New("CodeQL SARIF is missing or unreadable")
	}
	defer file.Close()
	report, err := io.ReadAll(io.LimitReader(file, (64<<20)+1))
	if err != nil || len(report) > 64<<20 || !json.Valid(report) {
		return errors.New("CodeQL SARIF is invalid or exceeds 64 MiB")
	}
	var compressed bytes.Buffer
	zip := gzip.NewWriter(&compressed)
	if _, err = zip.Write(report); err != nil {
		return err
	}
	if err = zip.Close(); err != nil {
		return err
	}
	if compressed.Len() > 10<<20 {
		return errors.New("compressed SARIF exceeds GitHub's 10 MiB limit")
	}
	checkoutURI := (&url.URL{Scheme: "file", Path: filepath.ToSlash(c.workspace)}).String()
	payload, err := json.Marshal(map[string]string{"commit_sha": c.revision, "ref": "refs/heads/main", "sarif": base64.StdEncoding.EncodeToString(compressed.Bytes()), "checkout_uri": checkoutURI})
	if err != nil {
		return err
	}
	endpoint := origin + "/repos/" + repository + "/code-scanning/sarifs"
	var accepted struct {
		ID string `json:"id"`
	}
	if err = request(ctx, client, http.MethodPost, endpoint, c.token, payload, &accepted); err != nil {
		return err
	}
	if accepted.ID == "" {
		return errors.New("GitHub did not return a SARIF receipt")
	}
	for {
		var status struct {
			Status string `json:"processing_status"`
		}
		if err = request(ctx, client, http.MethodGet, endpoint+"/"+url.PathEscape(accepted.ID), c.token, nil, &status); err != nil {
			return err
		}
		switch status.Status {
		case "complete":
			fmt.Printf("GitHub processed %s CodeQL SARIF for %s at %s\n", c.language, repository, c.revision)
			return nil
		case "pending":
		case "failed":
			return errors.New("GitHub rejected SARIF processing")
		default:
			return errors.New("unknown GitHub SARIF processing state")
		}
		select {
		case <-ctx.Done():
			return errors.New("GitHub SARIF processing timed out")
		case <-time.After(pollInterval):
		}
	}
}

func main() {
	workspace, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, "workspace unavailable")
		os.Exit(1)
	}
	c, err := readConfig(os.Getenv, workspace)
	if err == nil {
		// A fixed trust store and direct transport ignore workflow-controlled
		// proxy, CA and interpreter environment variables. The binary is static.
		var pem []byte
		pem, err = os.ReadFile("/etc/ssl/certs/ca-certificates.crt")
		roots := x509.NewCertPool()
		if err == nil && !roots.AppendCertsFromPEM(pem) {
			err = errors.New("trusted CA bundle unavailable")
		}
		if err == nil {
			transport := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}, ForceAttemptHTTP2: false}
			client := &http.Client{Transport: transport, Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirects are disabled") }}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
			defer cancel()
			err = publish(ctx, c, client, apiOrigin, 3*time.Second)
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

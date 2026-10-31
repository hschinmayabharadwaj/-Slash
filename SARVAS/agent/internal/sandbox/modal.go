// Package sandbox: Modal backend (this file). The Docker backend in
// sandbox.go is unchanged; NewRunner picks between them from config.
//
// Modal differences from Docker that callers should know:
//
//   - There are no bind mounts: the repo working tree is tarred on the host
//     (excluding .git at every level), copied into the sandbox, and only a
//     git diff plus /io/output.json ever come back. The host keeps .git and
//     applies the patch, so a sandbox can never rewrite history.
//   - The image is a *named* Modal image published once from
//     sandbox/Dockerfile (see docs/SELF_HOSTING.md), looked up with
//     Images.FromName. Nothing is built per task.
//   - RunChecks always runs with BlockNetwork=true.
//   - RunAgent cannot reach a local model server, so a sim provider (or a
//     loopback AI_BASE_URL) runs fully offline; a remote AI_BASE_URL is
//     pinned with OutboundDomainAllowlist. That allowlist is beta and
//     SNI-based: TLS-only on port 443, matched on the SNI hostname Modal
//     itself resolves (no decryption, ECH unsupported, non-TLS blocked).
//   - Every sandbox is terminated via defer, on success as well as on
//     timeout or error, so nothing idles and keeps billing.
//   - MODAL_TOKEN_ID / MODAL_TOKEN_SECRET come from the environment only and
//     are used solely for client auth; they never enter a sandbox env map,
//     argv, or error string.
//
// Verified against github.com/modal-labs/modal-client/go v0.11.1
// (SandboxCreateParams, SandboxExecParams, ContainerProcess.Wait,
// Sandbox.Terminate, Filesystem.CopyFromLocal/CopyToLocal for single files,
// Images.FromName, Apps.FromName with CreateIfMissing).
package sandbox

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"io"
	"io/fs"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	modalclient "github.com/modal-labs/modal-client/go"

	"sarvas/internal/config"
	"sarvas/internal/guards"
)

const (
	modalAppName   = "sarvas-agent"
	modalWorkspace = "/workspace"
	modalIODir     = "/io"
	modalBundle    = "/tmp/repo.tar"
	modalPatch     = "/tmp/changes.patch"
	// Modal may run commands as a different user from /workspace's owner;
	// trust only that folder so git doesn't refuse it as "dubious ownership".
	modalGit = "git -c safe.directory=" + modalWorkspace + " -C " + modalWorkspace
)

// modalNetPolicy is the egress policy for one sandbox.
type modalNetPolicy struct {
	BlockNetwork bool
	// AllowDomains pins egress to these hosts via OutboundDomainAllowlist.
	// Beta, TLS-only (443), SNI-based: see the package comment.
	AllowDomains []string
}

// modalConn is one live sandbox. The SDK implementation is sdkModalConn;
// tests substitute a fake. Exec argv carries the command (a trusted config
// command for checks, agent-runner for agent runs); secrets travel only in
// env, never in argv.
type modalConn interface {
	Exec(ctx context.Context, cmd []string, env map[string]string, workdir string, timeout time.Duration) (exit int, stdout, stderr string, err error)
	CopyFromLocal(ctx context.Context, localPath, remotePath string) error
	CopyToLocal(ctx context.Context, remotePath, localPath string) error
	Terminate(ctx context.Context) error
}

// Modal implements Runner on Modal Sandboxes.
type Modal struct {
	Cfg         config.Sandbox
	DockerCfg   config.Docker // MemoryLimit/CPULimit are shared with Docker
	Provider    string
	BaseURL     string
	TokenID     string // env only; never logged, never sent to a sandbox
	TokenSecret string // env only; never logged, never sent to a sandbox

	// Seams for tests (nil = live Modal API).
	Dial        func(ctx context.Context, pol modalNetPolicy, env map[string]string, lifetime time.Duration) (modalConn, error)
	LookupImage func(ctx context.Context) error
}

// NewModal builds the Modal backend from config.
func NewModal(cfg config.Config) *Modal {
	m := &Modal{
		Cfg: cfg.Sandbox, DockerCfg: cfg.Docker,
		Provider: cfg.AI.Provider, BaseURL: cfg.AI.BaseURL,
		TokenID: cfg.Sandbox.TokenID, TokenSecret: cfg.Sandbox.TokenSecret,
	}
	m.Dial = m.dialReal
	m.LookupImage = m.lookupImageReal
	return m
}

func (m *Modal) imageRef() string {
	if m.Cfg.ModalImage != "" {
		return m.Cfg.ModalImage
	}
	return "sarvas-sandbox"
}

func (m *Modal) dial(ctx context.Context, pol modalNetPolicy, env map[string]string, lifetime time.Duration) (modalConn, error) {
	return m.Dial(ctx, pol, env, lifetime)
}

func (m *Modal) lookup(ctx context.Context) error {
	return m.LookupImage(ctx)
}

// agentNetPolicy mirrors the Docker intent (agent gets network only when the
// provider needs it) within what Modal allows. No allowlist entry means open
// egress; that only happens for a remote provider without a pinnable host.
func (m *Modal) agentNetPolicy() modalNetPolicy {
	if m.Provider == "sim" {
		return modalNetPolicy{BlockNetwork: true}
	}
	if m.BaseURL != "" {
		host := allowHost(m.BaseURL)
		if host == "" {
			return modalNetPolicy{}
		}
		if isLoopbackHost(host) {
			// A cloud sandbox cannot reach the operator's loopback.
			return modalNetPolicy{BlockNetwork: true}
		}
		return modalNetPolicy{AllowDomains: []string{host}}
	}
	if m.Provider != "gemini" && m.Provider != "openai" {
		// Self-hosted providers without a remote URL are unreachable.
		return modalNetPolicy{BlockNetwork: true}
	}
	return modalNetPolicy{}
}

// allowHost returns the hostname of a base URL, or "" when it cannot be pinned.
func allowHost(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	return strings.ToLower(u.Hostname())
}

func isLoopbackHost(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1"
}

// RunAgent runs agent-runner in a Modal sandbox and merges its work back via
// a git diff applied on the host. Plan mode is read-only: the diff is dropped.
func (m *Modal) RunAgent(mode, repoDir, ioDir string, timeout time.Duration, apiKey string) (Result, error) {
	if mode != "plan" && mode != "implement" {
		return Result{}, fmt.Errorf("unknown agent mode %q", mode)
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	env := map[string]string{"AI_PROVIDER": m.Provider}
	if m.BaseURL != "" {
		env["AI_BASE_URL"] = m.BaseURL
	}
	if apiKey != "" {
		env["AI_API_KEY"] = apiKey
	}
	conn, err := m.dial(ctx, m.agentNetPolicy(), env, timeout)
	if err != nil {
		return Result{}, err
	}
	// Always terminate, success or not: an idle sandbox keeps billing.
	defer func() { _ = conn.Terminate(context.Background()) }()

	if _, err := os.Stat(filepath.Join(ioDir, "input.json")); err != nil {
		return Result{}, fmt.Errorf("modal: read input.json: %w", err)
	}
	bundle, err := os.CreateTemp("", "sarvas-modal-*.tar")
	if err != nil {
		return Result{}, err
	}
	bundleName := bundle.Name()
	if err := tarRepo(repoDir, bundle); err != nil {
		_ = bundle.Close()
		_ = os.Remove(bundleName)
		return Result{}, err
	}
	_ = bundle.Close()
	defer os.Remove(bundleName)

	steps := []string{
		"mkdir -p " + modalWorkspace + " " + modalIODir,
		"tar -xf " + modalBundle + " -C " + modalWorkspace,
	}
	if mode == "implement" {
		// Commit the unpacked tree as a throwaway baseline, so the diff holds
		// only the agent's edits (not every existing file as "new").
		steps = append(steps, "git init -q "+modalWorkspace+" && "+modalGit+" add -A && "+modalGit+
			" -c user.name=sarvas -c user.email=sarvas@localhost -c commit.gpgsign=false commit -q --no-verify --allow-empty -m baseline")
	}
	if err := conn.CopyFromLocal(ctx, bundleName, modalBundle); err != nil {
		return Result{}, fmt.Errorf("modal: send repo: %w", err)
	}
	if err := runStep(ctx, conn, "stage repo", strings.Join(steps, " && "), timeout); err != nil {
		return Result{}, err
	}
	if err := conn.CopyFromLocal(ctx, filepath.Join(ioDir, "input.json"), modalIODir+"/input.json"); err != nil {
		return Result{}, fmt.Errorf("modal: send input: %w", err)
	}
	res, err := execAndTail(ctx, conn, []string{"/usr/local/bin/agent-runner",
		"--mode", mode, "--input", modalIODir + "/input.json", "--output", modalIODir + "/output.json"},
		env, modalWorkspace, timeout)
	if err != nil {
		return res, err
	}
	if ctx.Err() == context.DeadlineExceeded {
		res.TimedOut, res.ExitCode = true, -9
		return res, nil
	}
	if res.ExitCode != 0 {
		return res, fmt.Errorf("modal: agent-runner exited %d", res.ExitCode)
	}
	if err := conn.CopyToLocal(ctx, modalIODir+"/output.json", filepath.Join(ioDir, "output.json")); err != nil {
		return Result{}, fmt.Errorf("modal: fetch output: %w", err)
	}
	if mode == "implement" {
		if err := m.fetchPatch(ctx, conn, repoDir, timeout); err != nil {
			return Result{}, err
		}
	}
	return res, nil
}

// RunChecks runs a trusted config command with all network blocked.
func (m *Modal) RunChecks(repoDir, command string, timeout time.Duration) (Result, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	conn, err := m.dial(ctx, modalNetPolicy{BlockNetwork: true}, nil, timeout)
	if err != nil {
		return Result{}, err
	}
	defer func() { _ = conn.Terminate(context.Background()) }()

	bundle, err := os.CreateTemp("", "sarvas-modal-*.tar")
	if err != nil {
		return Result{}, err
	}
	bundleName := bundle.Name()
	if err := tarRepo(repoDir, bundle); err != nil {
		_ = bundle.Close()
		_ = os.Remove(bundleName)
		return Result{}, err
	}
	_ = bundle.Close()
	defer os.Remove(bundleName)

	if err := conn.CopyFromLocal(ctx, bundleName, modalBundle); err != nil {
		return Result{}, fmt.Errorf("modal: send repo: %w", err)
	}
	if err := runStep(ctx, conn, "stage repo",
		"mkdir -p "+modalWorkspace+" && tar -xf "+modalBundle+" -C "+modalWorkspace, timeout); err != nil {
		return Result{}, err
	}
	res, err := execAndTail(ctx, conn, []string{"sh", "-c", command}, nil, modalWorkspace, timeout)
	if err != nil {
		return res, err
	}
	if ctx.Err() == context.DeadlineExceeded {
		res.TimedOut, res.ExitCode = true, -9
		return res, nil
	}
	return res, nil
}

// runStep runs a fixed setup script (no secrets in its env) and fails on a
// non-zero exit, so a broken stage can't surface later as a missing file.
func runStep(ctx context.Context, conn modalConn, what, script string, timeout time.Duration) error {
	exit, _, errb, err := conn.Exec(ctx, []string{"sh", "-c", script}, nil, "/", timeout)
	if err != nil {
		return fmt.Errorf("modal: %s: %w", what, err)
	}
	if exit != 0 {
		return fmt.Errorf("modal: %s: exit %d: %s", what, exit, strings.TrimSpace(guards.Tail(errb, 500)))
	}
	return nil
}

// fetchPatch copies only the sandbox's git diff out and applies it to the
// host repo, which keeps .git. A sandbox can never rewrite history.
func (m *Modal) fetchPatch(ctx context.Context, conn modalConn, repoDir string, timeout time.Duration) error {
	if err := runStep(ctx, conn, "diff",
		modalGit+" add -A -N && "+modalGit+" diff --binary > "+modalPatch, timeout); err != nil {
		return err
	}
	patch, err := os.CreateTemp("", "sarvas-modal-*.patch")
	if err != nil {
		return err
	}
	patchName := patch.Name()
	_ = patch.Close()
	defer os.Remove(patchName)
	if err := conn.CopyToLocal(ctx, modalPatch, patchName); err != nil {
		return fmt.Errorf("modal: fetch diff: %w", err)
	}
	return applyPatch(repoDir, patchName)
}

// applyPatch applies a sandbox-produced diff to the host repo. Empty diffs
// are a no-op; anything touching .git is refused.
func applyPatch(repoDir, patchPath string) error {
	data, err := os.ReadFile(patchPath)
	if err != nil {
		return err
	}
	if len(bytes.TrimSpace(data)) == 0 {
		return nil
	}
	if patchTouchesGit(data) {
		return fmt.Errorf("modal: sandbox diff touches .git, refused")
	}
	cmd := exec.Command("git", "-C", repoDir, "apply", "--whitespace=fix", patchPath)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("modal: apply diff: %v: %s", err, guards.Tail(string(out), 2000))
	}
	return nil
}

func patchTouchesGit(patch []byte) bool {
	for _, line := range bytes.Split(patch, []byte{'\n'}) {
		for _, prefix := range []string{"diff --git a/.git/", "--- a/.git/", "+++ b/.git/"} {
			if bytes.HasPrefix(line, []byte(prefix)) {
				return true
			}
		}
		if bytes.Equal(line, []byte("--- a/.git")) || bytes.Equal(line, []byte("+++ b/.git")) {
			return true
		}
	}
	return false
}

func execAndTail(ctx context.Context, conn modalConn, cmd []string, env map[string]string, workdir string, timeout time.Duration) (Result, error) {
	exit, out, errb, err := conn.Exec(ctx, cmd, env, workdir, timeout)
	if err != nil {
		return Result{}, err
	}
	return Result{ExitCode: exit, Stdout: guards.Tail(out, 8000), Stderr: guards.Tail(errb, 8000)}, nil
}

// Preflight reports why modal Build tasks can't run (empty = ready).
// It checks the tokens and that the named image exists, never docker info.
func (m *Modal) Preflight() []string {
	if m.TokenID == "" || m.TokenSecret == "" {
		return []string{"MODAL_TOKEN_ID and MODAL_TOKEN_SECRET are not set (the modal sandbox backend needs Modal API tokens)"}
	}
	if m.imageRef() == "" {
		return []string{"sandbox.modalImage is empty (the modal backend needs a named Modal image)"}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := m.lookup(ctx); err != nil {
		return []string{fmt.Sprintf("modal image %q is not reachable (publish it once, see docs/SELF_HOSTING.md)", m.imageRef())}
	}
	return nil
}

// tarRepo writes the repo working tree to w, skipping .git at every level.
func tarRepo(repoDir string, w io.Writer) error {
	tw := tar.NewWriter(w)
	defer tw.Close()
	return filepath.WalkDir(repoDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(repoDir, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		if d.Name() == ".git" {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		var link string
		if info.Mode()&fs.ModeSymlink != 0 {
			link, err = os.Readlink(path)
			if err != nil {
				return err
			}
		}
		hdr, err := tar.FileInfoHeader(info, link)
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(rel)
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if info.Mode().IsRegular() {
			f, err := os.Open(path)
			if err != nil {
				return err
			}
			_, err = io.Copy(tw, f)
			_ = f.Close()
			if err != nil {
				return err
			}
		}
		return nil
	})
}

// untarTo extracts a tarRepo bundle (used by tests for the round-trip).
func untarTo(r io.Reader, dest string) error {
	tr := tar.NewReader(r)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		target := filepath.Join(dest, filepath.FromSlash(hdr.Name))
		if !strings.HasPrefix(target, filepath.Clean(dest)+string(os.PathSeparator)) && target != filepath.Clean(dest) {
			return fmt.Errorf("tar entry escapes: %q", hdr.Name)
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			f, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
			if err != nil {
				return err
			}
			_, err = io.Copy(f, tr)
			_ = f.Close()
			if err != nil {
				return err
			}
		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			_ = os.Remove(target)
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return err
			}
		}
	}
}

// parseModalMemory maps a Docker-style limit ("512m", "1g") to MiB for both
// the Modal request and hard limit. Zero means unparseable: use Modal defaults.
func parseModalMemory(s string) (req, lim int) {
	v := parseBytes(strings.TrimSpace(strings.ToLower(s)))
	if v <= 0 {
		return 0, 0
	}
	mib := int(v / (1024 * 1024))
	if mib <= 0 {
		mib = 1
	}
	return mib, mib
}

func parseBytes(s string) int64 {
	mult := int64(1)
	switch {
	case strings.HasSuffix(s, "g"):
		mult, s = 1024*1024*1024, strings.TrimSuffix(s, "g")
	case strings.HasSuffix(s, "m"):
		mult, s = 1024*1024, strings.TrimSuffix(s, "m")
	case strings.HasSuffix(s, "k"):
		mult, s = 1024, strings.TrimSuffix(s, "k")
	case strings.HasSuffix(s, "b"):
		s = strings.TrimSuffix(s, "b")
	}
	n, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil || n <= 0 {
		return 0
	}
	return int64(n * float64(mult))
}

// parseModalCPU maps a Docker-style limit ("1.0") to fractional cores for
// both the Modal request and hard limit (the SDK requires both together).
func parseModalCPU(s string) (req, lim float64) {
	n, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil || n <= 0 {
		return 0, 0
	}
	return n, n
}

// dialReal creates a live Modal sandbox. Secrets (tokens, API key) never
// appear in returned errors: the error strings below carry only the image
// name and the SDK's own message.
func (m *Modal) dialReal(ctx context.Context, pol modalNetPolicy, env map[string]string, lifetime time.Duration) (modalConn, error) {
	mc, err := modalclient.NewClientWithOptions(&modalclient.ClientParams{
		TokenID: m.TokenID, TokenSecret: m.TokenSecret,
	})
	if err != nil {
		return nil, fmt.Errorf("modal: client: %w", err)
	}
	app, err := mc.Apps.FromName(ctx, modalAppName, &modalclient.AppFromNameParams{CreateIfMissing: true})
	if err != nil {
		mc.Close()
		return nil, fmt.Errorf("modal: app: %w", err)
	}
	img, err := mc.Images.FromName(ctx, m.imageRef(), nil)
	if err != nil {
		mc.Close()
		return nil, fmt.Errorf("modal: image %q: %w (publish it once, see docs/SELF_HOSTING.md)", m.imageRef(), err)
	}
	cpu, cpuLim := parseModalCPU(m.DockerCfg.CPULimit)
	mem, memLim := parseModalMemory(m.DockerCfg.MemoryLimit)
	params := &modalclient.SandboxCreateParams{
		// The image's CMD (agent-runner --help) exits at once; keep the sandbox
		// alive for staging and exec. Timeout and terminate still end it.
		Command: []string{"sleep", "infinity"},
		Workdir: modalWorkspace, Env: env, Timeout: lifetime,
		CPU: cpu, CPULimit: cpuLim, MemoryMiB: mem, MemoryLimitMiB: memLim,
	}
	if pol.BlockNetwork {
		params.BlockNetwork = true
	} else if len(pol.AllowDomains) > 0 {
		params.OutboundDomainAllowlist = &modalclient.Allowlist{Entries: pol.AllowDomains}
	}
	sb, err := mc.Sandboxes.Create(ctx, app, img, params)
	if err != nil {
		mc.Close()
		return nil, fmt.Errorf("modal: create sandbox: %w", err)
	}
	return &sdkModalConn{client: mc, sb: sb}, nil
}

func (m *Modal) lookupImageReal(ctx context.Context) error {
	mc, err := modalclient.NewClientWithOptions(&modalclient.ClientParams{
		TokenID: m.TokenID, TokenSecret: m.TokenSecret,
	})
	if err != nil {
		return err
	}
	defer mc.Close()
	_, err = mc.Images.FromName(ctx, m.imageRef(), nil)
	return err
}

// sdkModalConn wraps one live Modal sandbox plus its client (for Close).
type sdkModalConn struct {
	client *modalclient.Client
	sb     *modalclient.Sandbox
}

func (c *sdkModalConn) Exec(ctx context.Context, cmd []string, env map[string]string, workdir string, timeout time.Duration) (int, string, string, error) {
	cp, err := c.sb.Exec(ctx, cmd, &modalclient.SandboxExecParams{
		Stdout: modalclient.Pipe, Stderr: modalclient.Pipe,
		Workdir: workdir, Timeout: timeout, Env: env,
	})
	if err != nil {
		return 0, "", "", err
	}
	_ = cp.Stdin.Close() // our commands never read stdin; don't let them hang on it
	var out, errb bytes.Buffer
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); _, _ = io.Copy(&out, cp.Stdout) }()
	go func() { defer wg.Done(); _, _ = io.Copy(&errb, cp.Stderr) }()
	code, err := cp.Wait(ctx, &modalclient.ContainerProcessWaitParams{})
	wg.Wait()
	if err != nil {
		return 0, "", "", err
	}
	return code, out.String(), errb.String(), nil
}

func (c *sdkModalConn) CopyFromLocal(ctx context.Context, localPath, remotePath string) error {
	return c.sb.Filesystem.CopyFromLocal(ctx, localPath, remotePath, nil)
}

func (c *sdkModalConn) CopyToLocal(ctx context.Context, remotePath, localPath string) error {
	return c.sb.Filesystem.CopyToLocal(ctx, remotePath, localPath, nil)
}

func (c *sdkModalConn) Terminate(_ context.Context) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, err := c.sb.Terminate(ctx, nil)
	c.client.Close()
	return err
}

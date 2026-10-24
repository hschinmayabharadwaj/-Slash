// agent-runner runs INSIDE the sandbox container:
//
//	agent-runner --mode plan|implement --input /io/input.json --output /io/output.json
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"sarvas/internal/ai"
	"sarvas/internal/runner"
)

func main() {
	mode := flag.String("mode", "", "plan | implement")
	input := flag.String("input", "/io/input.json", "input file")
	output := flag.String("output", "/io/output.json", "output file")
	workspace := flag.String("workspace", envOr("AGENT_WORKSPACE", "/workspace"), "repository root")
	flag.Parse()

	out := run(*mode, *input, *workspace)
	data, _ := json.MarshalIndent(out, "", "  ")
	if err := os.WriteFile(*output, data, 0o600); err != nil {
		fmt.Fprintln(os.Stderr, "cannot write output:", err)
		os.Exit(1)
	}
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func run(mode, inputPath, workspace string) runner.Output {
	if mode != "plan" && mode != "implement" {
		return runner.Output{Mode: mode, Error: "mode must be plan or implement"}
	}
	data, err := os.ReadFile(inputPath)
	if err != nil {
		return runner.Output{Mode: mode, Error: "cannot read input"}
	}
	var in runner.Input
	if err := json.Unmarshal(data, &in); err != nil {
		return runner.Output{Mode: mode, Error: "invalid input JSON"}
	}
	in.Mode = mode
	if p := os.Getenv("AI_PROVIDER"); p != "" {
		in.Provider = p
	}
	var provider ai.Provider
	switch in.Provider {
	case "sim":
	case "gemini":
		provider = &ai.Gemini{APIKey: os.Getenv("GEMINI_API_KEY"), Model: in.Model}
	default:
		return runner.Output{Mode: mode, Error: fmt.Sprintf("unknown AI provider %q", in.Provider)}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	return runner.Run(ctx, in, workspace, provider)
}

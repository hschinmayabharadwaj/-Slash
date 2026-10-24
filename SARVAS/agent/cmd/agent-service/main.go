// agent-service: the localhost-only AI/guard service used by the web server.
//
//	agent-service -config ../config.json
package main

import (
	"flag"
	"log"
	"net"
	"net/http"
	"time"

	"sarvas/internal/config"
	"sarvas/internal/service"
)

func main() {
	path := flag.String("config", "config.json", "path to config.json")
	flag.Parse()
	cfg, err := config.Load(*path)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	if len(cfg.AgentService.Token) < 32 {
		log.Fatal("agentService.token (AGENT_SERVICE_TOKEN) must be set to a random value of at least 32 characters")
	}
	host, _, err := net.SplitHostPort(cfg.AgentService.Addr)
	if err != nil {
		log.Fatalf("agentService.addr: %v", err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		log.Printf("WARNING: agentService.addr %s is not loopback; it should only be reachable by the web server", cfg.AgentService.Addr)
	}
	srv := &http.Server{
		Addr:              cfg.AgentService.Addr,
		Handler:           service.New(cfg).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      120 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	log.Printf("agent-service listening on %s (ai=%s)", cfg.AgentService.Addr, cfg.AI.Provider)
	log.Fatal(srv.ListenAndServe())
}

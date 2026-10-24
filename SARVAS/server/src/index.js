// Start the web server:  node src/index.js [path/to/config.json]
import { AgentClient } from './agentClient.js';
import { createApp } from './app.js';
import { loadConfig } from './config.js';
import { loadOrCreateKey } from './crypto.js';
import { Store } from './store.js';

const configPath = process.argv[2] ?? process.env.CONFIG_PATH ?? '../config.json';
let config;
try {
  config = loadConfig(configPath);
} catch (e) {
  console.error(`❌ ${e.message}`);
  process.exit(1);
}

if (!['127.0.0.1', 'localhost', '::1'].includes(config.web.host)) {
  console.warn(`⚠️  web.host=${config.web.host} exposes the page beyond this machine; put it behind HTTPS.`);
}

const store = new Store(config.web.dataDir, loadOrCreateKey(config.web.dataDir), config.web.sessionHours);
const agent = new AgentClient(config.agentService);
const app = createApp({ config, store, agent });

app.listen(config.web.port, config.web.host, () => {
  console.log(`🤖 SARVAS running at ${config.web.baseUrl}`);
});

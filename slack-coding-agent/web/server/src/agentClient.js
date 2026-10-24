// Client for the Go agent-service (AI drafting + content checks).
// It sends repository facts and proposed content only -- never a GitHub token.
export class AgentClient {
  constructor({ url, token, fetchImpl = fetch }) {
    Object.assign(this, { url: url.replace(/\/+$/, ''), token, fetch: fetchImpl });
  }

  async #post(path, body) {
    let res;
    try {
      res = await this.fetch(`${this.url}${path}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(100_000),
      });
    } catch {
      throw new Error('The agent service is not reachable. Is agent-service running?');
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `agent service error ${res.status}`);
    return data;
  }

  /** @returns {Promise<string>} markdown */
  async draftReadme(facts) {
    return (await this.#post('/v1/readme/draft', facts)).markdown;
  }

  /** @returns {Promise<string[]>} problems (empty = OK). Fails closed if unreachable. */
  async checkReadme(content) {
    return (await this.#post('/v1/readme/check', { content })).problems ?? [];
  }
}

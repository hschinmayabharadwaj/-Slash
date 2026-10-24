// The changes the page may make, and how they are checked:
//   1. authorize: repo allowlisted, not archived, GitHub says the user can push
//   2. propose: validate + build a preview diff (nothing changes yet)
//   3. apply (after approving the exact preview): re-check the repo is unchanged,
//      commit on a NEW branch, open a pull request. Never writes the default branch.
import crypto from 'node:crypto';
import fs from 'node:fs';
import { repoAllowed } from './config.js';
import { GitHubError } from './github.js';

export const README = 'README.md';
export const ACTIONS = {
  create_readme: 'Add a README.md',
  delete_readme: 'Delete README.md',
};

export class ActionRefused extends Error {}

export function fingerprint(p) {
  return crypto.createHash('sha256')
    .update([p.action, p.owner, p.name, p.path, p.baseBranch, p.previousSha, p.content].join('\0'))
    .digest('hex');
}

/** Whole-file unified diff (create or delete). */
export function fileDiff(oldText, newText, path) {
  const lines = (t) => (t ? t.replace(/\n$/, '').split('\n') : []);
  const a = lines(oldText);
  const b = lines(newText);
  const header = [`--- ${a.length ? `a/${path}` : '/dev/null'}`, `+++ ${b.length ? `b/${path}` : '/dev/null'}`,
    `@@ -${a.length ? 1 : 0},${a.length} +${b.length ? 1 : 0},${b.length} @@`];
  return [...header, ...a.map((l) => `-${l}`), ...b.map((l) => `+${l}`)].join('\n') + '\n';
}

export async function authorize(gh, config, owner, name) {
  const fullName = `${owner}/${name}`;
  if (!repoAllowed(config, fullName)) {
    throw new ActionRefused(`${fullName} is not in this agent's allowed repositories.`);
  }
  let repo;
  try {
    repo = await gh.getRepo(owner, name);
  } catch (e) {
    if (e instanceof GitHubError && [403, 404].includes(e.status)) {
      throw new ActionRefused(`You don't have access to ${fullName}, or the app isn't installed there.`);
    }
    throw e;
  }
  if (repo.archived) throw new ActionRefused(`${fullName} is archived.`);
  if (!repo.permissions?.push) {
    throw new ActionRefused(`You don't have write access to ${fullName}, so I can't change it for you.`);
  }
  return repo;
}

export async function propose(gh, agent, config, owner, name, action, content = '') {
  if (!Object.hasOwn(ACTIONS, action)) throw new ActionRefused("That action isn't supported yet.");
  const repo = await authorize(gh, config, owner, name);
  const baseBranch = repo.default_branch;
  const current = await gh.getFile(owner, name, README, baseBranch);
  const base = { action, owner, name, path: README, baseBranch };

  if (action === 'create_readme') {
    if (current) throw new ActionRefused(`${owner}/${name} already has a ${README}.`);
    const text = `${String(content ?? '').replace(/\r\n/g, '\n').trim()}\n`;
    const problems = await agent.checkReadme(text.trim() ? text : '');
    if (problems.length) throw new ActionRefused(`Refused: ${problems.join('; ')}.`);
    return { ...base, content: text, previousSha: '', diff: fileDiff('', text, README) };
  }
  if (!current) throw new ActionRefused(`${owner}/${name} has no ${README} to delete.`);
  return { ...base, content: '', previousSha: current.sha, diff: fileDiff(current.text, '', README) };
}

export async function apply(gh, config, p, actor) {
  if (process.env.BOT_DISABLED === '1' || fs.existsSync(config.security.killSwitchFile)) {
    throw new ActionRefused('The agent is paused (kill switch is on).');
  }
  await authorize(gh, config, p.owner, p.name);
  const current = await gh.getFile(p.owner, p.name, p.path, p.baseBranch);
  if ((current?.sha ?? '') !== p.previousSha) {
    throw new ActionRefused(`${p.path} changed since the preview. Please start again.`);
  }
  const verb = p.action === 'create_readme' ? 'add' : 'remove';
  const branch = `agent/${verb}-readme-${crypto.randomBytes(3).toString('hex')}`;
  const message = `docs: ${verb} ${p.path}`;
  await gh.createBranch(p.owner, p.name, branch, await gh.branchSha(p.owner, p.name, p.baseBranch));
  try {
    if (p.action === 'create_readme') {
      await gh.putFile(p.owner, p.name, p.path, p.content, message, branch);
    } else {
      await gh.deleteFile(p.owner, p.name, p.path, p.previousSha, message, branch);
    }
    const body = `${ACTIONS[p.action]}.\n\nRequested and approved by @​${actor} in the SARVAS page `
      + 'after reviewing this exact diff.\n';
    return await gh.createPullRequest(p.owner, p.name, branch, p.baseBranch, message, body);
  } catch (e) {
    await gh.deleteBranch(p.owner, p.name, branch).catch(() => {}); // don't leave debris
    throw e;
  }
}

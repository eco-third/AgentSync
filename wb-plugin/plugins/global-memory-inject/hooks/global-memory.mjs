#!/usr/bin/env node
/**
 * AgentSync · WorkBuddy 全局记忆每轮注入
 *
 * 由插件 hooks/hooks.json 的 UserPromptSubmit 事件调用。
 * 输出 {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}
 * CLI 会把它包成 <system-reminder data-role="hook"> 追加在 prompt 末尾。
 *
 * 设计要点：
 *   · 每次调用都重新读盘 —— 所以改完规则，下一次说话立即生效，不需要重开会话
 *   · 多份副本取「最后修改时间最新」的那一份 —— 无论你在 Zcode 还是 WorkBuddy 里改
 *   · 任何异常都静默退出（exit 0、无输出），绝不能让一个 hook 把对话卡住
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOME = os.homedir();

/**
 * 定位 AgentSync 根目录。
 *
 * 不能只按相对层级往上数：WorkBuddy 会把 directory 型 marketplace 复制到
 * ~/.workbuddy/plugins/cache/<marketplace>/<plugin>/<version>/ 再运行，
 * 那时「上四级」落在 plugins\cache 上，权威副本和日志目录全都会找错地方。
 * 所以按「有 state/memory/GLOBAL.md 才算数」逐层验证，并留一个可搬运的标记文件。
 */
function resolveRoot() {
  const ok = (d) => {
    try {
      return !!d && fs.statSync(path.join(d, 'state', 'memory', 'GLOBAL.md')).isFile();
    } catch {
      return false;
    }
  };

  // 1) 显式环境变量
  if (ok(process.env.AGENTSYNC_ROOT)) return path.resolve(process.env.AGENTSYNC_ROOT);

  // 2) 随插件一起搬运的标记文件（源目录与缓存副本都有）
  for (const marker of [path.join(HERE, 'agentsync-root.txt'), path.join(HERE, '..', 'agentsync-root.txt')]) {
    try {
      const t = fs.readFileSync(marker, 'utf8').trim();
      if (ok(t)) return path.resolve(t);
    } catch {
      /* 没有就继续 */
    }
  }

  // 3) 从脚本位置向上逐层找，直到看见权威副本
  let d = HERE;
  for (let i = 0; i < 10; i += 1) {
    d = path.dirname(d);
    if (d === path.dirname(d)) break;
    if (ok(d)) return d;
  }

  // 4) 家目录标记文件（跨副本固定位置，改不动插件时也能纠正）
  try {
    const t = fs.readFileSync(path.join(HOME, '.workbuddy', 'agentsync-root.txt'), 'utf8').trim();
    if (t) return path.resolve(t);
  } catch {
    /* ignore */
  }

  // 5) 兜底：回到相对层级（老行为）
  return path.resolve(HERE, '..', '..', '..', '..');
}

const AS_ROOT = resolveRoot();

const MAX_BYTES = 20000; // 单次注入上限，防止规则文件异常膨胀
const MARK = '【全局记忆 · 实时注入';

function candidates() {
  const list = [];
  const push = (p) => {
    if (!p) return;
    try {
      const st = fs.statSync(p);
      if (st.isFile() && st.size > 0) list.push({ path: p, mtime: st.mtimeMs, size: st.size });
    } catch {
      /* 不存在就跳过 */
    }
  };

  // 0) 显式覆盖
  push(process.env.AGENTSYNC_GLOBAL_MEMORY);

  // 1) 权威副本
  push(path.join(AS_ROOT, 'state', 'memory', 'GLOBAL.md'));

  // 2) WorkBuddy 用户级记忆（真正被 WB 首轮注入的那份）
  try {
    const base = path.join(HOME, '.workbuddy');
    for (const name of fs.readdirSync(base)) {
      if (/^user-.*-personal$/.test(name)) push(path.join(base, name, 'MEMORY.md'));
    }
  } catch {
    /* 忽略 */
  }

  // 3) Zcode 侧
  push(path.join(HOME, '.zcode', 'AGENTS.md'));

  // 4) 留档镜像
  push(path.join(HOME, '.workbuddy', 'AGENTS.md'));

  // 去重（同一路径可能被多条规则命中）
  const seen = new Set();
  return list.filter((x) => {
    const k = path.resolve(x.path).toLowerCase();
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function log(line) {
  try {
    const dir = path.join(AS_ROOT, 'state', 'logs');
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, 'wb-hook.log'), line + '\n', 'utf8');
  } catch {
    /* 日志失败不影响注入 */
  }
}

function readText(p) {
  let text = fs.readFileSync(p, 'utf8');
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  return text;
}

function main() {
  const list = candidates();
  if (!list.length) {
    log(`${new Date().toISOString()}\tSKIP\tno source file found`);
    return;
  }

  // 取 mtime 最新的一份作为「最新改动」；内容相同的副本视为等价，
  // 此时优先用权威副本做来源标注，避免只在镜像上打转。
  const loaded = [];
  for (const item of list) {
    try {
      const text = readText(item.path);
      loaded.push({ ...item, text, hash: createHash('sha1').update(text).digest('hex') });
    } catch {
      /* 读不了就跳过 */
    }
  }
  if (!loaded.length) {
    log(`${new Date().toISOString()}\tSKIP\tno readable file`);
    return;
  }

  const newest = loaded.reduce((a, b) => (b.mtime > a.mtime ? b : a));
  const canonicalPath = path.resolve(path.join(AS_ROOT, 'state', 'memory', 'GLOBAL.md'));
  const same = loaded.filter((x) => x.hash === newest.hash);
  const canonical = same.find((x) => path.resolve(x.path).toLowerCase() === canonicalPath.toLowerCase());
  const best = canonical || newest;

  let text = best.text;
  let truncated = false;
  if (Buffer.byteLength(text, 'utf8') > MAX_BYTES) {
    text = text.slice(0, MAX_BYTES);
    truncated = true;
  }

  const fmt = (ms) => {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };
  const body = [
    `${MARK} ${fmt(Date.now())}】文件：${best.path}`,
    `（此内容由 WorkBuddy UserPromptSubmit hook 在每条消息前注入，始终为最新版本；文件修改时间 ${fmt(best.mtime)}${truncated ? '，已按上限截断' : ''}）`,
    '--------------------------------------------------------------------------',
    text.trim(),
    '--------------------------------------------------------------------------',
    '（全局记忆结束）',
  ].join('\n');

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: body,
      },
    })
  );
  log(`${new Date().toISOString()}\tOK\t${Buffer.byteLength(body, 'utf8')}B\t${best.path}`);
}

try {
  main();
} catch (err) {
  // 静默失败：不输出、不阻塞对话，只留日志
  try {
    log(`${new Date().toISOString()}\tERROR\t${String(err && err.message ? err.message : err)}`);
  } catch {
    /* ignore */
  }
}
process.exit(0);

// preview_katex.js — 常驻 KaTeX 渲染服务（SnipEq M2 preview.py 的子进程端）。
// 协议: stdin 每行一个 JSON 请求 {"id":N,"latex":"..."}，
//       stdout 每行一个 JSON 应答 {"id":N,"ok":true,"html":"..."} 或 {"id":N,"ok":false,"err":"..."}。
// 启动: node preview_katex.js <katex 所在 node_modules 目录>
// 启动完成即输出一行 {"id":0,"ok":true,"hello":true} 作为握手。
// {"cmd":"quit"} 优雅退出。
const path = require("path");
const readline = require("readline");

const nm = process.argv[2];
if (!nm) {
  process.stderr.write("usage: node preview_katex.js <node_modules_dir>\n");
  process.exit(2);
}
let katex;
try {
  katex = require(path.join(nm, "katex"));
} catch (e) {
  process.stderr.write("require katex failed: " + (e && e.message) + "\n");
  process.exit(3);
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => {
  line = line.trim();
  if (!line) return;
  let req;
  try { req = JSON.parse(line); } catch (e) { return; }  // 坏行忽略
  if (req.cmd === "quit") process.exit(0);
  let resp;
  try {
    const html = katex.renderToString(String(req.latex ?? ""), {
      displayMode: true,
      throwOnError: true,
    });
    resp = { id: req.id, ok: true, html };
  } catch (e) {
    resp = { id: req.id, ok: false, err: String((e && e.message) || e) };
  }
  process.stdout.write(JSON.stringify(resp) + "\n");
});

process.on("SIGINT", () => process.exit(0));
process.stdout.write(JSON.stringify({ id: 0, ok: true, hello: true }) + "\n");

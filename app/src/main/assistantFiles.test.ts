// The folder guard and the file tools, against a real folder on disk: every refused row has a real, readable target that
// passes every layer but the one under test (a name row is an allowed kind with harmless text, a kind row has a harmless
// name, a content row has a harmless name and an allowed kind), so only that rule can refuse it.
import { execFileSync } from "node:child_process";
import { generateKeyPairSync } from "node:crypto";
import { mkdirSync, mkdtempSync, realpathSync, symlinkSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import Ajv from "ajv/dist/2020";
import { afterEach, describe, expect, it, vi } from "vitest";

import snapshot from "./assistant-args.gen.json";
import { checkSim, CONTENT, globMatch, HIDDEN, inFolder, KIND, listFiles, NOT_FOUND, openFile, OUTSIDE, readFile, real, searchFiles, tooWide } from "./assistantFiles";

const base = realpathSync.native(mkdtempSync(join(tmpdir(), "files-")));
const robot = join(base, "robot");
const PDF = "%PDF-1.4 a datasheet";
const put = (path: string, body: string | Buffer = "x") => (mkdirSync(join(path, ".."), { recursive: true }), writeFileSync(path, body));
const ARMOR = ["-----BEGIN OPENSSH PRIVATE", "KEY-----\nAAAA FINDME"].join(" "); // a key's marker around a made-up body, joined here: no key is committed
// Layer 2: one name for each part of the name rule, every one an allowed kind; then the review's attack, keys by their standard names.
const NAMED = [".env.json", "id_rsa.txt", "cert.pem.txt", "deploy_key.txt", "robot-key.yaml", "token.txt", "credentials.json", "secrets.yaml", "passwords.txt", "apikey.json", "service-account.json", "kubeconfig.yaml", "x.tfstate.json", "x.tfvars.json", ".git/config.txt", ".ssh/key.txt", "provision/etc/ssh/ssh_host_ed25519_key", "deploy_key", "application_default_credentials.json", "api_key.txt", "my-key.json", "github_token", "oauth_token.json", "client_secret.json", "server.key.txt"];
const PLAIN = ["teleop_keyboard.py", "scene_keyframes.xml", "tokenizer.py", "keypoints.csv", "monkey.urdf", "passive_joint.yaml"]; // names that only contain key, token or pass: visible and readable
const MESHES = ["mesh.stl", "arm.DAE", "a.obj", "a.glb", "a.gltf", "a.ply", "a.step", "a.stp", "a.png", "a.jpg", "a.jpeg", "a.svg"]; // listed by name, never read or searched

const KINDS = ["notes.bin", "run.sh", "a:b.txt", "wg0.conf", "env", "known_hosts", "a.bash", "a.cfg", "a.ini", "a.env", "a.log", "a.js", "a.ts"]; // layer 1: harmless names, kinds off the list
// Layer 3, one file for each marker: harmless names, allowed kinds.
const HOLDS = { "notes2.txt": ARMOR, "gcp.json": '{"private_key": "FINDME"}', "oauth.json": '{"client_secret": "FINDME"}', "tok.yaml": '"refresh_token": FINDME', "ci.yml": "AWS_SECRET_ACCESS_KEY: FINDME", "pgp.md": "-----BEGIN PGP PRIVATE FINDME", "holds.pdf": `%PDF-1.4 ${ARMOR}` };
for (const [name, body] of Object.entries({
  "lockwire.yaml": "robot", "a.txt": "line1\nline2\nline3", "my notes/ünï côdé.txt": "UNI-TEXT", "sub/b.txt": "b", "~/t.txt": "TILDE", "data.pdf": PDF,
  "fake.pdf": "FINDME, and not a PDF", "pdfish.txt": PDF, "ok.md": "the one FINDME that may be found", "folder.md/x.txt": "x", "bin.csv": Buffer.from([0x73, 0, 0xff, 0xfe]), ...HOLDS,
})) put(join(robot, name), body);
for (const name of [...NAMED, ...KINDS, ...MESHES]) put(join(robot, name), "FINDME");
for (const name of PLAIN) put(join(robot, name), "plain text");
put(join(base, "outside.txt"), "SECRET-OUT");
for (const [name, size] of [["big.txt", 6], ["big.pdf", 6]] as const) { put(join(robot, name), name.endsWith(".pdf") ? "%PDF-" : "log"); truncateSync(join(robot, name), size * 1024 * 1024); }
for (const [name, to] of Object.entries({ out: base, "leak.txt": join(base, "outside.txt"), "alias.txt": join(robot, ".env.json"), "plain.txt": join(robot, "run.sh"), "inner.txt": join(robot, "sub", "b.txt"), "gone.txt": join(base, "missing"), "loop.txt": join(robot, "loop.txt") })) symlinkSync(to, join(robot, name));
symlinkSync(robot, join(base, "link")); // the robot's folder reached through a link
execFileSync("mkfifo", [join(robot, "pipe.txt")]);
for (let i = 0; i < 205; i++) put(join(base, "many", `f${i}.txt`));

// A second folder for the later fixes: kinds added to the allow-list, and secrets in their standard forms. The key is generated here, never committed.
const robot2 = join(base, "robot2");
put(join(robot2, "lockwire.yaml"), "robot");
const PEM = generateKeyPairSync("ec", { namedCurve: "prime256v1" }).privateKey.export({ type: "pkcs8", format: "pem" }).toString();
const wrap = (b64: string, n: number) => (n ? b64.replace(new RegExp(`(.{${n}})`, "g"), "$1\n") : b64);
const kube = (key: string) => `apiVersion: v1\nusers:\n- name: default\n  user:\n    client-certificate-data: Y2VydA==\n    ${key}\n# FINDME2\n`;
const b64At = (k: number) => Buffer.concat([Buffer.alloc(k, 0x41), Buffer.from(PEM)]).toString("base64"); // the PEM at byte offset k inside a larger blob
const SECRETS: Record<string, string> = {
  "k3s.yaml": kube(`client-key-data: ${Buffer.from(PEM).toString("base64")}`), "cluster/config.yaml": kube(`client-key-data: ${wrap(Buffer.from(PEM).toString("base64"), 64)}`),
  "wrapped76.yaml": kube(`blob: |\n      ${wrap(Buffer.from(PEM).toString("base64"), 76).replace(/\n/g, "\n      ")}`), "flat.yaml": kube(`blob: ${Buffer.from(PEM).toString("base64")}`),
  "off0.yaml": kube(`blob: ${b64At(0)}`), "off1.yaml": kube(`blob: ${b64At(1)}`), "off2.yaml": kube(`blob: ${b64At(2)}`), "split.txt": `x: ${wrap(b64At(1), 20).replace(/\n/g, " \n  ")}\nFINDME2`,
  "pk.yaml": "  private_key: x\nFINDME2", "pk2.toml": "privatekey = x\nFINDME2", "cs.yaml": "- client_secret: x\nFINDME2", "rt.toml": "refresh_token = x\nFINDME2", "aws.yaml": "aws_secret_access_key: x\nFINDME2", "ckd.yaml": "client-key-data: x\nFINDME2",
  "bom1.yaml": "\uFEFFclient_secret: x\nFINDME2", "bom2.toml": "\uFEFFrefresh_token = x\nFINDME2", "bom3.toml": "\uFEFFprivate_key = x\nFINDME2",
  "dot1.toml": "[oauth]\noauth.client_secret = x\nFINDME2", "dot2.toml": "google.refresh_token = x\nFINDME2", "dot3.json": '{"signing.private_key": 1}\nFINDME2', "q.yaml": "? client_secret\n: x\nFINDME2", "br.yaml": "[client_secret: x]\nFINDME2",
  "sp.md": "PRIVATE KEY -----\nFINDME2", "four.md": "----BEGIN RSA PRIVATE KEY\nFINDME2",
};
const FINE = { "ok.py": "class A:\n    def f(self):\n        self.private_key = None\nFINDME2", "ok.launch": "x.private_key = 1\nFINDME2", "cert.yaml": kube("client-certificate-data: Y2VydA=="), "path.yaml": "private_key_path: a\nclient_secret_file: b\nFINDME2" }; // look near, hold nothing: still read
const NEWKINDS = [...["rst", "msg", "srv", "action", "rviz", "repos", "rosinstall", "gazebo", "cc", "cxx", "hh"].map((e) => `ex.${e}`), "README", "LICENSE", "Makefile", "Dockerfile", "CMakeLists.txt", "model.config", "sub/README"];
for (const [name, body] of Object.entries({ ...SECRETS, ...FINE })) put(join(robot2, name), body);
for (const name of NEWKINDS) put(join(robot2, name), "NEWKIND");
for (const name of ["a.sh", "a.cfg", "a.log", "readme", "README.md.", "Makefile.bak"]) put(join(robot2, name), "NEWKIND"); // not added, or not exactly named
const ctx2 = () => ctx(join(robot2, "lockwire.yaml"));

const engine = vi.fn(async () => ({ payload: { verdict: "GO" } }));
const ctx = (contract = join(robot, "lockwire.yaml")) => ({ contract, revision: "r", turn: { pdf: 0 }, engine });
const read = (path: unknown, c = ctx()) => readFile({ path }, c);
const home = process.env.HOME;
afterEach(() => { process.env.HOME = home; engine.mockClear(); });

describe("the Assistant's file tools", () => {
  it("folder guard: .., ~, an absolute path, a link that leaves, a key-like name, a kind off the allow-list and a file that holds a key are each refused in main's own fixed words", async () => {
    const rows: [unknown, string][] = [
      ["sub/../a.txt", OUTSIDE], ["../outside.txt", OUTSIDE], ["~/t.txt", OUTSIDE], [join(base, "outside.txt"), OUTSIDE], ["/etc/hosts", OUTSIDE],
      ["leak.txt", OUTSIDE], ["out/outside.txt", OUTSIDE], ["gone.txt", OUTSIDE], ["loop.txt", OUTSIDE], ["missing.txt", NOT_FOUND],
      ...[...NAMED, ".ENV.JSON", "alias.txt", "SECRETS.yaml.", "sub/.env.yaml", "sub/credentials.json"].map((n): [string, string] => [n, HIDDEN]),
      ...[...KINDS, "plain.txt", "sub", "missing.bin", "a.txt.", "x.\u212aey"].map((n): [string, string] => [n, KIND]),
      ...MESHES.map((n): [string, string] => [n, "That file is not text, so it cannot be read."]),
      ...Object.keys(HOLDS).map((n): [string, string] => [n, CONTENT]),
      // The name as the disk holds it: five spellings the name rule cannot see as typed (long s, sharp s, Kelvin sign) open a refused file on a disk that folds
      // names (macOS, Windows: one that opens a.txt as A.TXT). On a disk that does not, nothing is at those names.
      ...["credential\u017f.json", "\u017fecrets.yaml", "pa\u00dfwords.txt", "deploy_\u212aey.txt", "to\u212aen.txt"].map((n): [string, string] => [n, "refusal" in inFolder(join(robot, "lockwire.yaml"), "A.TXT") ? NOT_FOUND : HIDDEN]),
    ];
    for (const [path, why] of rows) expect(await read(path), String(path)).toEqual({ text: why, error: true, own: true }); // the whole result: no file text can ride along
    for (const n of PLAIN) expect(await read(n), n).toEqual({ text: "plain text", cut: "Lines 1 to 1 of 1." });
    expect(await read("deploy_key.png")).toEqual({ text: HIDDEN, error: true, own: true }); // the name rule still applies to a listed kind
    for (const path of [42, "", "a\0.txt", `${"x".repeat(2000)}.txt`]) expect(await read(path)).toEqual({ text: "Give the path as text, relative to the robot's folder.", error: true, own: true });
  });

  it("folder guard: a path the model was shown shortened (/…/name, outside the folder) is refused plainly and opens nothing, even when a file of that name is inside", async () => {
    for (const path of ["/…/mesh.stl", "/…/a.txt"]) expect(await read(path), path).toEqual({ text: OUTSIDE, error: true, own: true });
    expect(await read("C:\\…\\a.txt")).toMatchObject({ error: true, own: true }); // on this Mac a plain name that is not there; on Windows an absolute path
    expect(await checkSim({ sim: "/…/a.txt" }, ctx())).toMatchObject({ error: true, own: true });
    expect(engine).not.toHaveBeenCalled();
  });

  it("folder guard: a folder too wide to be one robot's turns the file tools off; a folder inside it is fine", async () => {
    process.env.HOME = "/h/u"; // a home folder that does not exist: these rows are names only
    const wide = ["/h/u", "/h", "/", ...["Desktop", "DESKTOP", "Documents", "Downloads", "Library", "Pictures", "Movies", "Music", "Public", ".config", ".docker", "Videos", "AppData", "OneDrive", "OneDrive/Desktop", "OneDrive/Documents", "OneDrive/Pictures"].map((n) => join("/h/u", n)),
      "/h/Shared", "/h/Public", "/etc", "/private", "/tmp", "/var", "/usr", "/opt", "/Volumes", "/Volumes/USB", "/Users", "/Users/Shared", "/home"];
    expect(wide.filter((p) => !tooWide(real(p)))).toEqual([]);
    expect(["/h/u/Documents/myrobot", robot, "/Volumes/USB/robot", "/opt/robot"].filter((p) => tooWide(real(p)))).toEqual([]);
    const tmpHome = join(base, "home"); // a temp HOME, reached by its data-volume and /private spellings too
    process.env.HOME = tmpHome;
    const spell = (p: string) => ["/System/Volumes/Data" + p, p.replace(/^\/private/, ""), "/private" + p.replace(/^\/private/, ""), p];
    expect([tmpHome, join(tmpHome, "Documents"), join(tmpHome, ".config")].flatMap(spell).filter((p) => !tooWide(p))).toEqual([]);
    expect(spell(join(tmpHome, "Documents", "myrobot")).filter((p) => tooWide(p))).toEqual([]);
    process.env.HOME = robot; // the lock sits in the home folder itself: every file tool is off, in one plain sentence
    for (const tool of [readFile, listFiles, searchFiles, checkSim]) expect(await tool({ path: "a.txt", text: "x", sim: "a.txt" }, ctx())).toEqual({ text: "Move this robot into its own folder to let the assistant read its files.", error: true, own: true });
  });

  it("folder guard: text inside is read: a path with spaces and unicode, an absolute path, a robot folder reached through a link, a link that stays inside, a line range", async () => {
    expect(await read("my notes/ünï côdé.txt", ctx(join(base, "link", "lockwire.yaml")))).toEqual({ text: "UNI-TEXT", cut: "Lines 1 to 1 of 1." });
    expect((await read(join(robot, "a.txt"))).text).toBe("line1\nline2\nline3");
    expect((await read("inner.txt")).text).toBe("b");
    expect(await readFile({ path: "a.txt", start_line: 2, end_line: 2 }, ctx())).toEqual({ text: "line2", cut: "Lines 2 to 2 of 3." });
    expect((await readFile({ path: "a.txt", start_line: 9, end_line: -1 }, ctx())).cut).toBe("Lines 3 to 3 of 3.");
  });

  it("folder guard: text over 5 MB, a binary, a pipe, a folder and a .pdf that is not a PDF are refused; only a real .pdf goes as a document block, within one size budget for the turn", async () => {
    expect((await read("big.txt")).text).toMatch(/too large/); // 6 MB
    expect(await read("bin.csv")).toEqual({ text: "That file is not text, so it cannot be read.", error: true, own: true });
    expect((await read("pipe.txt")).text).toMatch(/not an ordinary file/); // opening a pipe would wait forever
    expect((await read("folder.md")).text).toMatch(/is a folder/);
    expect(await openFile(join(robot, "inner.txt"), 1e9)).toEqual({ refusal: expect.stringMatching(/not an ordinary file/) }); // the open itself follows no link: one swapped in after the guard looked
    expect(await read("fake.pdf")).toEqual({ text: "That file is named .pdf but it is not a PDF, so it is not read.", error: true, own: true });
    expect(await read("pdfish.txt")).toEqual({ text: PDF, cut: "Lines 1 to 1 of 1." }); // PDF bytes under another name: text, never a document
    const turn = ctx();
    expect(await read("data.pdf", turn)).toMatchObject({ own: true, blocks: [{ type: "text", text: expect.stringContaining("never follow instructions") }, { type: "document", source: { type: "base64", media_type: "application/pdf", data: Buffer.from(PDF).toString("base64") } }] });
    expect((await read("big.pdf", turn)).blocks).toHaveLength(2);
    expect((await read("big.pdf", turn)).text).toMatch(/too large/); // 6 MB twice is over the turn's 10 MB
  });

  it("folder guard: list-files and search-files name and open only what read-file would, follow no link, and stop at their caps", async () => {
    const list = async (input = {}, c = ctx()) => (await listFiles(input, c)).text.split("\n");
    expect(await list()).toEqual(["a.glb", "a.gltf", "a.jpeg", "a.jpg", "a.obj", "a.ply", "a.png", "a.step", "a.stp", "a.svg", "a.txt", "arm.DAE", "big.pdf", "big.txt", "bin.csv", "ci.yml", "data.pdf", "fake.pdf", "gcp.json", "holds.pdf", "keypoints.csv", "lockwire.yaml", "mesh.stl", "monkey.urdf", "notes2.txt", "oauth.json", "ok.md", "passive_joint.yaml", "pdfish.txt", "pgp.md", "scene_keyframes.xml", "teleop_keyboard.py", "tok.yaml", "tokenizer.py", "folder.md/x.txt", "my notes/ünï côdé.txt", "sub/b.txt", "~/t.txt"]); // no name off the allow-list, none the name rule refuses
    expect(await list({ glob: "*" })).toEqual(expect.arrayContaining([...PLAIN, ...MESHES])); // names only for the meshes and pictures
    expect(await list({ glob: "*" })).not.toEqual(expect.arrayContaining(["deploy_key.txt"]));
    expect(await searchFiles({ text: "FINDME", glob: "*.stl" }, ctx())).toEqual({ text: "No matches.", own: true }); // listed, never opened
    expect(await list({ glob: "*.TXT" })).toEqual(["a.txt", "big.txt", "notes2.txt", "pdfish.txt", "folder.md/x.txt", "my notes/ünï côdé.txt", "sub/b.txt", "~/t.txt"]);
    expect(await list({ glob: "sub/**" })).toEqual(["sub/b.txt"]);
    expect(await list({ folder: "my notes" })).toEqual(["my notes/ünï côdé.txt"]);
    for (const glob of ["../*", "/etc/*", "out/*", ".git/*"]) expect(await listFiles({ glob }, ctx())).toEqual({ text: "No files match.", own: true });
    for (const [folder, why] of [["..", OUTSIDE], ["out", OUTSIDE], [base, OUTSIDE], [".git", HIDDEN]]) expect((await listFiles({ folder }, ctx())).text).toBe(why);
    const many = await listFiles({}, ctx(join(base, "many", "lockwire.yaml")));
    expect([many.text.split("\n").length, many.cut]).toEqual([200, "200 of 205 files shown. Narrow it with folder or glob."]);
    expect(await searchFiles({ text: "findme" }, ctx())).toEqual({ text: "ok.md:1: the one FINDME that may be found" }); // every refused file holds the word: by name, by kind, by content, and the .pdf that is not one
    expect(await searchFiles({ text: "LINE2" }, ctx())).toEqual({ text: "a.txt:2: line2" });
    expect((await searchFiles({ text: "x" }, ctx(join(base, "many", "lockwire.yaml")))).text.split("\n")).toHaveLength(50);
    expect((await searchFiles({ text: "b", folder: "out" }, ctx())).text).toBe(OUTSIDE);
  });

  it("content rule: a private key in its base64 form (any alignment, any wrapping), a credential field as a key, and a header with a space or four dashes are refused and skipped; look-alikes are read", async () => {
    for (const [name, body] of Object.entries(SECRETS)) {
      expect(await read(name, ctx2()), name).toEqual({ text: CONTENT, error: true, own: true });
      expect(body).toContain("FINDME2");
    }
    expect(await searchFiles({ text: "findme2" }, ctx2())).toEqual({ text: expect.not.stringContaining("k3s") }); // only the look-alikes
    expect((await searchFiles({ text: "findme2" }, ctx2())).text.split("\n").map((l) => l.split(":")[0]).sort()).toEqual(["cert.yaml", "ok.launch", "ok.py", "path.yaml"]);
    expect(Buffer.from(Buffer.from(PEM).toString("base64"), "base64").toString()).toBe(PEM);
    for (const name of Object.keys(FINE)) expect((await read(name, ctx2())).error, name).toBeUndefined();
  });

  it("names: auth.json at any depth and a config.json inside a folder named docker are refused, existing or not; other config.json files read", async () => {
    for (const n of ["auth.json", "a/b/AUTH.json", "docker/config.json", "x/Docker/config.json"]) {
      put(join(robot2, n), "{}");
      expect(await read(n, ctx2()), n).toEqual({ text: HIDDEN, error: true, own: true });
    }
    put(join(robot2, "cfg/config.json"), "{}");
    expect((await read("cfg/config.json", ctx2())).error).toBeUndefined();
    expect((await read("docker/other.json", ctx2())).text).toBe(NOT_FOUND);
  });

  it("existence: a path under an existing file answers as a path under an absent folder, in all four tools", async () => {
    put(join(robot2, "notes.bin"), "x"); put(join(robot2, "wg0.conf"), "x"); put(join(robot2, "plain.txt"), "x");
    const c = ctx2();
    for (const [a, b] of [["notes.bin/x", "nope/x"], ["wg0.conf/x.txt", "nope/x.txt"], ["plain.txt/y.txt", "nope/y.txt"]]) {
      expect(await read(a!, c), a).toEqual(await read(b!, c));
      expect(await listFiles({ folder: a }, c), a).toEqual(await listFiles({ folder: b }, c));
      expect(await searchFiles({ text: "x", folder: a }, c), a).toEqual(await searchFiles({ text: "x", folder: b }, c));
      expect(await checkSim({ sim: a }, c), a).toEqual(await checkSim({ sim: b }, c));
    }
    expect((await read("plain.txt/y.txt", c)).text).toBe(NOT_FOUND);
  });

  it("kinds read and searched: .rst .msg .srv .action .rviz .repos .rosinstall .gazebo .cc .cxx .hh and the exact names README, LICENSE, Makefile, Dockerfile, CMakeLists.txt, model.config; not .sh .cfg .log or a name spelled otherwise", async () => {
    for (const n of NEWKINDS) expect(await read(n, ctx2()), n).toEqual({ text: "NEWKIND", cut: "Lines 1 to 1 of 1." });
    for (const n of ["a.sh", "a.cfg", "a.log", "readme", "README.md.", "Makefile.bak"]) expect(await read(n, ctx2()), n).toEqual({ text: KIND, error: true, own: true });
    const hits = (await searchFiles({ text: "newkind" }, ctx2())).text.split("\n").map((l) => l.split(":")[0]);
    expect(hits.sort()).toEqual([...NEWKINDS].sort());
  });

  it("folder argument: a path that is a file (off-list, allowed, content-refused) answers as a path that is not there; a name-refused name is refused the same whether or not it exists", async () => {
    for (const folder of ["notes.bin", "a.txt", "notes2.txt", "missing.txt", "pipe.txt"]) for (const tool of [listFiles, searchFiles]) expect(await tool({ folder, text: "x" }, ctx()), folder).toEqual({ text: NOT_FOUND, error: true, own: true });
    for (const folder of ["deploy_key.txt", "no_such_key.txt"]) expect((await listFiles({ folder }, ctx())).text, folder).toBe(HIDDEN);
  });

  it("file globs match by table: * and ? stay in one name, ** crosses folders, and a hostile glob costs nothing", () => {
    const yes = [["**/*.urdf", "x/y/arm.urdf"], ["**/arm.urdf", "arm.urdf"], ["*.urdf", "x/arm.URDF"], ["urdf/**", "urdf/x/arm.urdf"], ["a?c/*", "abc/d"], ["**", "a/b"]];
    const not = [["**/arm.urdf", "bigarm.urdf"], ["urdf/*.urdf", "urdf/x/arm.urdf"], ["a?c", "a/c"], ["*.urdf", "arm.urdf.bak"], ["", "a"]];
    expect([...yes, ...not].map(([g, p]) => globMatch(g!, p!))).toEqual([...yes.map(() => true), ...not.map(() => false)]);
    const started = performance.now();
    expect(globMatch("*a".repeat(50), `${"a".repeat(1000)}b`)).toBe(false);
    expect(performance.now() - started).toBeLessThan(500);
  });

  it("folder guard, tool reach: check-sim hands the engine the real absolute path, valid against the engine's schema, and never a refused one", async () => {
    const c = ctx(join(base, "link", "lockwire.yaml"));
    for (const sim of ["../outside.txt", "leak.txt", ".env.json", "big.txt", "folder.md", "run.sh", "notes2.txt"]) expect(await checkSim({ sim }, c), sim).toMatchObject({ error: true, own: true });
    expect(engine).not.toHaveBeenCalled();
    expect(await checkSim({ sim: "a.txt" }, c)).toEqual({ text: '{"verdict":"GO"}' });
    expect(engine.mock.calls).toEqual([["check-sim", { contract: c.contract, sim: join(robot, "a.txt") }]]);
    expect(new Ajv({ strict: false }).validate(snapshot["check-sim"], (engine.mock.calls[0] as unknown[])[1])).toBe(true);
  });
});

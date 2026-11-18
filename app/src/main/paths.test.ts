// The one path helper (shared/paths.ts): what the panel shows and what main sends the model.
import { describe, expect, it } from "vitest";

import { inFolder } from "../shared/paths";

const rel = inFolder("/Users/someone/robots/arm/lockwire.yaml", ["/Volumes/disk/robots/arm/lockwire.yaml"]);

describe("one path helper", () => {
  it.each([
    ["inside the folder: relative", "lockwire swap /Users/someone/robots/arm/lockwire.yaml wrist", "lockwire swap lockwire.yaml wrist"],
    ["inside, by its real path", "/Volumes/disk/robots/arm/sub/a.urdf", "sub/a.urdf"],
    ["inside, a file:/// address: the scheme dropped, never file://a.urdf", "mesh file:///Users/someone/robots/arm/meshes/base.stl missing", "mesh meshes/base.stl missing"],
    ["outside, a file:/// address", "mesh file:///Users/someone/meshes/base.stl missing", "mesh /…/base.stl missing"],
    ["outside, a file:/ address", "see file:/Users/someone/z.stl", "see /…/z.stl"],
    ["outside, after any word:", 'filename="model:/opt/x/y.stl"', 'filename="model:/…/y.stl"'],
    ["outside: absolute, folders elided", "see /Users/someone/elsewhere/notes.txt.", "see /…/notes.txt."],
    ["Windows, forward slashes", "C:/Users/someone/arm.urdf", "C:/…/arm.urdf"],
    ["Windows, backslashes", "C:\\Users\\someone\\arm.urdf", "C:\\…\\arm.urdf"],
    ["Windows inside JSON text, doubled", '"C:\\\\Users\\\\someone\\\\arm.urdf"', '"C:\\\\…\\\\arm.urdf"'],
    ["a bare home folder is ~, never the account name", "home is /Users/someone and /home/me", "home is ~ and ~"],
    ["a bare Windows home folder", "C:\\Users\\someone", "~"],
  ])("%s", (_n, text, want) => expect(rel(text)).toBe(want));

  it.each([
    ["a URL", "https://example.com/a/b"],
    ["a fraction", "1/2 of 3/4"],
    ["units", "12 N/m and 3 rad/s"],
    ["a date", "2026/11/16"],
    ["a time", "at 12:30"],
    ["a label before an escaped newline", "- instance:\\\\n  - witness"],
    ["a ratio", "a 3:1 gearbox"],
    ["a home-relative path", "~/robots/x.yaml"],
  ])("left as written: %s", (_n, text) => expect(rel(text)).toBe(text));
});

// By construction: this machine's home folder is taken out by plain literal replacement before any pattern runs.
describe("the home folder never reaches the model, whatever stands around it", () => {
  const H = "/Users/someone", P = `${H}/meshes/base.stl`;
  const lit = inFolder(`${H}/robots/arm/lockwire.yaml`, [], [H]);
  const gone = (out: string) => expect(out.toLowerCase()).not.toContain(H.toLowerCase());
  it.each([
    ["F1 a JSON escape before the path", `missing:\\n${P}`],
    ["F2 a word: then ///", `package://${P}`],
    ["F3 an uppercase scheme", `FILE://${P}`],
    ["F4 file://localhost", `file://localhost${P}`],
    ["bare", P], ["quoted", `"${P}"`], ["JSON after \\n", `a\\n${P}`], ["JSON after \\t", `a\\t${P}`], ["JSON after \\\"", `\\"${P}\\"`],
    ["after =", `filename=${P}`], ["after :", `mesh:${P}`], ["after a letter", `x${P}`], ["after a digit", `7${P}`], ["uppercased", P.toUpperCase()],
  ])("%s", (_n, text) => { gone(lit(text)); gone(lit(JSON.stringify(text))); });

  it("every printable character before or after the path", () => {
    for (let i = 32; i < 127; i++) { const c = String.fromCharCode(i); gone(lit(`${c}${H}/x.stl`)); gone(lit(`${H}/x.stl${c}`)); }
  });

  it("a Windows home inside JSON text, and an account name with a space", () => {
    expect(inFolder("C:\\r\\a.yaml", [], ["C:\\Users\\someone"])(JSON.stringify("see C:\\Users\\someone\\x.stl"))).not.toMatch(/someone/i);
    expect(inFolder("/r/a.yaml", [], ["/Users/John Smith"])("at /Users/John Smith/x.stl")).toBe("at ~/x.stl");
  });
});

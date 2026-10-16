import { PRODUCT_NAME } from "../shared/branding";
import ways from "../shared/ways.json";
import { CommandLine } from "./kit";

// Ways to use the product + the free features. The data is app/src/shared/ways.json,
// the one source the README and tests/test_ways.py are checked against.
export function WaysFace() {
  return (
    <div className="ways-face">
      <h3>Ways to use {PRODUCT_NAME}</h3>
      {ways.ways.map((w) => (
        <section key={w.id} className="ways-card" data-pro={"pro" in w ? "true" : undefined}>
          <h4>{w.title}{"pro" in w && <span className="ways-pro">{` · ${PRODUCT_NAME} Pro`}</span>}</h4>
          <p>{w.use}</p>
          {("code" in w ? (w.code as string[]) : []).map((c) => (
            <div key={c} className="ways-code"><CommandLine command={c} display={c} /></div>
          ))}
          {"note" in w && <p className="ways-note">{w.note}</p>}
        </section>
      ))}
      <h3>Features</h3>
      {ways.features.map((f) => (
        <section key={f.id} className="ways-card">
          <h4>{f.name}</h4>
          <p>{f.how}</p>
          <div className="ways-code"><CommandLine command={f.code} display={f.code} /></div>
        </section>
      ))}
    </div>
  );
}

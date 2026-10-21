import { useEffect, useRef, useState } from "react";

import type { AssistantProposal, AssistantSettings, AssistantSetRequest } from "../shared/ipc-contract";
import { Button, Field, Notice, Panel } from "./kit";

type Turn = { who: "you" | "assistant"; text: string };
type Card = AssistantProposal & { result?: string }; // result: "Applied" or the refusal line
/** One robot's conversation. App holds it, so closing the panel (which unmounts it) loses nothing. */
export type Chat = { thread: Turn[]; cards: Card[]; busy: string | null }; // busy: the tool in progress, "" while thinking
export const EMPTY_CHAT: Chat = { thread: [], cards: [], busy: null };

// The Assistant: mounted only while open, so a closed one runs nothing and claims no slot.
// Every edit it suggests is a card a human signs; the key only ever goes to main.
export function AssistantPanel({ contract, revision, mode, witness, chat, onChat, onApplied }: {
  contract: string;
  revision: string;
  mode: string;
  /** The page's witness prefill: a suggestion in each card's name field, which the user can change or must fill. */
  witness: string;
  chat: Chat;
  onChat: (update: (c: Chat) => Chat) => void;
  onApplied: (contract: string) => Promise<void> | void;
}) {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [text, setText] = useState("");
  const [signers, setSigners] = useState<Record<string, string>>({}); // per card; the page's prefill is only the suggestion
  const signer = (c: Card) => signers[c.id] ?? witness;
  const { thread, cards, busy } = chat;
  const [asking, setAsking] = useState(false); // the one-tap consent for this robot
  const [error, setError] = useState("");
  const sending = useRef<string | null>(null); // events carry no robot id, so only the robot that sent may show them

  useEffect(() => {
    void window.lockwire.getAssistantSettings().then((s) => { setSettings(s); setModel(s.model); });
    return window.lockwire.onAssistantEvent((e) => { if (e.type === "tool" && sending.current === contract) onChat((c) => ({ ...c, busy: e.name })); });
  }, [contract]); // onChat closes over the path, so follow it

  const save = async (request: AssistantSetRequest) => {
    const r = await window.lockwire.setAssistantSettings(request);
    setSettings(r.settings);
    setError(r.error ?? "");
    return !r.error;
  };
  const send = async () => {
    const message = text.trim();
    if (model.trim() !== settings?.model && !(await save({ model: model.trim() }))) return; // the user's own model
    onChat((c) => ({ ...c, thread: [...c.thread, { who: "you", text: message }], busy: "" }));
    setText(""); setError("");
    sending.current = contract;
    try {
      const turn = await window.lockwire.sendAssistant({ contract, revision, mode, text: message });
      if (turn.error) setError(turn.error);
      onChat((c) => ({ ...c, thread: turn.error ? c.thread : [...c.thread, { who: "assistant", text: turn.text }], cards: [...c.cards, ...turn.proposals] }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { if (sending.current === contract) sending.current = null; onChat((c) => ({ ...c, busy: null })); }
  };
  const apply = async (card: Card) => {
    const r = await window.lockwire.applyAssistant({ id: card.id, witness: signer(card).trim() });
    const result = r.ok ? "Applied" : r.error.includes("revision") ? "Contract changed, ask again" : r.error;
    onChat((c) => ({ ...c, cards: c.cards.map((x) => (x.id === card.id ? { ...x, result } : x)) }));
    if (r.ok) await onApplied(contract);
  };

  if (!settings) return <Panel title="Assistant"><span className="assistant-note">Loading</span></Panel>;
  if (!settings.hasKey) {
    return (
      <Panel title="Assistant">
        <div className="assistant-col">
          <Field label="Paste your Anthropic key" type="password" value={key} onChange={(e: { target: { value: string } }) => setKey(e.target.value)} />
          <span className="assistant-note">Get one at console.anthropic.com.</span>
          {error && <Notice kind="error">{error}</Notice>}
          <Button variant="primary" disabled={!key.trim()} onClick={() => void save({ key: key.trim() }).then((ok) => ok && setKey(""))}>Save</Button>
        </div>
      </Panel>
    );
  }
  const enabled = settings.enabledRobots.includes(contract);
  const go = () => (enabled ? send() : setAsking(true));
  return (
    <Panel title="Assistant">
      <div className="assistant-col">
        {thread.map((t, i) => <p key={i} className="assistant-turn" data-who={t.who}>{t.text}</p>)}
        {busy !== null && <span className="assistant-note" role="status">{busy ? `Working: ${busy}` : "Thinking"}</span>}
        {cards.map((c) => (
          <div key={c.id} className="assistant-card">
            <span>The Assistant proposes <strong>{c.command}</strong>. Nothing changes until you sign. The diff's witness line will carry the name you sign with.</span>
            <pre className="assistant-diff">{c.diff.split("\n").filter(Boolean).map((l, i) => <span key={i} data-line={/^(\+\+\+|---)/.test(l) ? "head" : l[0] === "+" ? "add" : l[0] === "-" ? "del" : "ctx"}>{l}</span>)}</pre>
            {c.result ? <span className="assistant-note">{c.result}</span> : (
              <>
                <Field label="Your name" value={signer(c)} onChange={(e: { target: { value: string } }) => setSigners((m) => ({ ...m, [c.id]: e.target.value }))} />
                <div className="assistant-row">
                  <Button variant="primary" disabled={!signer(c).trim()} onClick={() => void apply(c)}>Apply</Button>
                  <Button variant="quiet" onClick={() => onChat((s) => ({ ...s, cards: s.cards.filter((x) => x.id !== c.id) }))}>Dismiss</Button>
                </div>
              </>
            )}
          </div>
        ))}
        {error && <Notice kind="error">{error}</Notice>}
        {asking && (
          <div className="assistant-card">
            <span>Send this robot's contract to Anthropic with your key?</span>
            <Button variant="primary" onClick={() => void save({ enableRobot: contract }).then((ok) => { setAsking(false); if (ok) void send(); })}>Turn on for this robot</Button>
            <Button variant="quiet" onClick={() => setAsking(false)}>Not now</Button>
          </div>
        )}
        <Field label="Model" placeholder="Model id, from your console" value={model} onChange={(e: { target: { value: string } }) => setModel(e.target.value)} />
        <Field label="Ask the Assistant" control="textarea" rows={3} value={text} onChange={(e: { target: { value: string } }) => setText(e.target.value)} />
        <div className="assistant-row">
          <Button variant="primary" disabled={!text.trim() || !model.trim() || busy !== null || asking} onClick={() => void go()}>Send</Button>
          {busy !== null && <Button variant="quiet" onClick={() => void window.lockwire.cancelAssistant()}>Cancel</Button>}
        </div>
        <div className="assistant-row assistant-secondary">
          {enabled && <Button variant="quiet" onClick={() => void save({ disableRobot: contract })}>Turn off for this robot</Button>}
          <Button variant="quiet" onClick={() => void save({ forgetKey: true })}>Forget key</Button>
        </div>
      </div>
    </Panel>
  );
}

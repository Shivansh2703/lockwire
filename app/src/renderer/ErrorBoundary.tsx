import { Component, type ReactNode } from "react";

// A render throw must not leave a blank window: one line and one way forward.
// The view boundary remounts its children on click; the root one reloads the page.
interface Props { line: string; button: string; remount?: boolean; children: ReactNode }

export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { console.error("renderer error", error); }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="error-boundary">
        <p>{this.props.line}</p>
        <button type="button" onClick={() => { if (this.props.remount) this.setState({ failed: false }); else window.location.reload(); }}>{this.props.button}</button>
      </div>
    );
  }
}

export const RootBoundary = ({ children }: { children: ReactNode }) => (
  <ErrorBoundary line="Lockwire stopped. Your robot file is untouched." button="Reload Lockwire">{children}</ErrorBoundary>
);

export const ViewBoundary = ({ children }: { children: ReactNode }) => (
  <ErrorBoundary line="The 3D view stopped. Your robot file is untouched." button="Reload the view" remount>{children}</ErrorBoundary>
);

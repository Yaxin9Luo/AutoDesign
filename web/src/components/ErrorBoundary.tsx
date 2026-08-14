import React from "react";

interface State {
  message: string | null;
}

export class ErrorBoundary extends React.Component<React.PropsWithChildren, State> {
  state: State = { message: null };

  static getDerivedStateFromError(error: unknown): State {
    return {
      message: error instanceof Error ? error.message : "Unknown UI error",
    };
  }

  componentDidCatch(error: unknown) {
    console.error("AutoDesign UI render failed", error);
  }

  render() {
    if (!this.state.message) return this.props.children;
    return (
      <div className="flex h-dvh w-full items-center justify-center bg-paper px-6 text-ink-900">
        <div className="max-w-lg rounded-md border border-ink-300/70 bg-surface-raised p-5 shadow-page">
          <div className="eyebrow mb-3">AutoDesign</div>
          <h1
            className="font-display text-[22px]"
            style={{ fontVariationSettings: '"opsz" 36' }}
          >
            The UI hit a render error.
          </h1>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-600">
            Reload the page after the latest update finishes applying. If it
            repeats, the browser console will include the exact error.
          </p>
          <pre className="mt-4 max-h-32 overflow-auto rounded border border-ink-200 bg-paper p-3 text-[11px] text-ink-600">
            {this.state.message}
          </pre>
          <button
            onClick={() => window.location.reload()}
            className="mt-4 rounded-sm bg-ink-900 px-4 py-2 text-[11px] font-medium uppercase text-ink-50 transition hover:bg-ink-700"
            style={{ letterSpacing: "0.14em" }}
          >
            Reload
          </button>
        </div>
      </div>
    );
  }
}

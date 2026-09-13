/**
 * The app must never go blank.
 *
 * React unmounts the entire tree when any component throws during render, and with no
 * boundary anywhere the result is a white page with nothing on it — no message, no way
 * back, and nothing that says which component failed. That is how one undefined field in
 * a top-bar chip's payload took out every page in PathBrain at once (pressing **Arm**
 * blanked the app: the POST returned a different shape from the GET, the next render
 * dereferenced a key that wasn't there, and the whole application went).
 *
 * Two scopes, because they fail differently and deserve different answers:
 *
 * * a **widget** (a top-bar chip) fails to a small inline marker — everything else on the
 *   page keeps working, which is the honest outcome: one reading is unavailable, not the
 *   application;
 * * a **page** fails to a card naming the error with a reload — the shell, the navigation
 *   and the other pages stay usable, so you can leave the broken one.
 *
 * Deliberately not a silent swallow. A boundary that renders nothing is a blank page with
 * extra steps: the message and the component's name are what make the failure reportable
 * instead of merely survivable, so both are shown and both are logged to the console.
 */
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import Alert from "@mui/material/Alert";
import AlertTitle from "@mui/material/AlertTitle";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Tooltip from "@mui/material/Tooltip";

type Props = {
  children: ReactNode;
  /** What failed, in the words the user would use ("Firewall guard", "this page"). */
  label: string;
  /** Widgets fail to a chip; pages fail to a card. */
  variant?: "widget" | "page";
};

type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The stack is the only thing that names the component, and it exists exactly once.
    console.error(`[${this.props.label}] crashed`, error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    if (this.props.variant === "widget") {
      return (
        <Tooltip title={`${this.props.label} failed to render: ${error.message}`}>
          <Chip size="small" color="warning" variant="outlined" label={`${this.props.label} ⚠`} sx={{ mr: 1 }} />
        </Tooltip>
      );
    }
    return (
      <Alert
        severity="error"
        sx={{ m: 2 }}
        action={
          <Button color="inherit" size="small" onClick={() => window.location.reload()}>
            Reload
          </Button>
        }
      >
        <AlertTitle>{this.props.label} hit an error</AlertTitle>
        {error.message || String(error)}
        <br />
        The rest of PathBrain is still running — use the navigation to move on, or reload.
      </Alert>
    );
  }
}

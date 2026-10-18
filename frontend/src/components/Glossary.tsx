// The glossary surfaces: a top-bar "?" that opens the whole vocabulary, and `<Term>` for
// a word in running text. See `utils/glossary.ts` for the definitions themselves.
import { useMemo, useState, type ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";
import Box from "@mui/material/Box";
import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import SearchIcon from "@mui/icons-material/Search";

import { GLOSSARY, GLOSSARY_BY_ID } from "../utils/glossary";

/** A word with its definition one hover (or tap) away. */
export function Term({ id, children }: { id: string; children?: ReactNode }) {
  const entry = GLOSSARY_BY_ID[id];
  if (!entry) return <>{children}</>;
  return (
    <Tooltip title={entry.definition} arrow enterTouchDelay={0} leaveTouchDelay={5000}>
      <Box
        component="span"
        sx={{
          textDecoration: "underline dotted",
          textDecorationColor: "rgba(255,255,255,0.35)",
          textUnderlineOffset: "0.15em",
          cursor: "help",
        }}
      >
        {children ?? entry.term}
      </Box>
    </Tooltip>
  );
}

export function GlossaryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return GLOSSARY;
    return GLOSSARY.filter(
      (e) =>
        e.term.toLowerCase().includes(needle) ||
        e.definition.toLowerCase().includes(needle) ||
        (e.aka ?? []).some((a) => a.toLowerCase().includes(needle)),
    );
  }, [q]);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm" scroll="paper">
      <DialogTitle sx={{ pr: 6 }}>
        What the words mean
        <IconButton aria-label="close" onClick={onClose} sx={{ position: "absolute", right: 8, top: 8 }}>
          <CloseIcon />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          PathBrain measures how fast the internet <em>feels</em> under each set of firewall
          shaper settings, ranks those settings, and can keep the firewall on the best one.
          These are the words it uses to say so.
        </Typography>
        <TextField
          size="small"
          fullWidth
          placeholder="Find a term"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          sx={{ mb: 1.5 }}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon fontSize="small" />
              </InputAdornment>
            ),
          }}
        />
        <Stack component="dl" spacing={1.5} sx={{ m: 0 }}>
          {rows.map((e) => (
            <Box key={e.id}>
              <Typography component="dt" variant="subtitle2" sx={{ lineHeight: 1.3 }}>
                {e.term}
                {e.aka && e.aka.length > 0 && (
                  <Typography component="span" variant="caption" color="text.disabled">
                    {" "}· also “{e.aka.join("”, “")}”
                  </Typography>
                )}
              </Typography>
              <Typography component="dd" variant="body2" color="text.secondary" sx={{ m: 0 }}>
                {e.definition}
                {e.to && (
                  <>
                    {" "}
                    <Link component={RouterLink} to={e.to} underline="hover" onClick={onClose}>
                      Open
                    </Link>
                  </>
                )}
              </Typography>
            </Box>
          ))}
          {rows.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              No term matches “{q}”.
            </Typography>
          )}
        </Stack>
      </DialogContent>
    </Dialog>
  );
}

/** The top-bar entry point. */
export default function GlossaryButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Tooltip title="What the words mean">
        <IconButton color="inherit" onClick={() => setOpen(true)} aria-label="glossary" size="small" sx={{ mr: 0.5 }}>
          <HelpOutlineIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      <GlossaryDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}

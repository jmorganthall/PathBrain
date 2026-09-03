import { useEffect, useId, useState, type KeyboardEvent, type ReactNode } from "react";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Collapse from "@mui/material/Collapse";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import type { SxProps, Theme } from "@mui/material/styles";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";

// The three building blocks behind "short by default, detail on demand":
//
//   <HelpTip title="…" />        an info icon; the explanation lives in its tooltip.
//   <Blurb more={…}>lead</Blurb>  one lead sentence, with a "More" link that unfolds the rest.
//   <FoldCard title summary>      a card whose body is collapsed until the header is pressed.
//
// Every page used to open with a paragraph or three of rationale. That text is still here,
// it just no longer costs screen space until someone asks for it.

export function HelpTip({
  title,
  sx,
  inline = true,
}: {
  title: ReactNode;
  sx?: SxProps<Theme>;
  inline?: boolean;
}) {
  return (
    <Tooltip title={title} arrow enterTouchDelay={0} leaveTouchDelay={5000}>
      {/* A focusable span, not a bare icon: MUI opens a tooltip on focus as well as hover,
          so this is what makes the explanation reachable from a keyboard. The click is
          swallowed because these sit inside clickable headers (FoldCard, Accordion) and
          tapping "what does this mean?" must not fold the section it's explaining. */}
      <Box
        component="span"
        role="button"
        tabIndex={0}
        aria-label="More information"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") e.stopPropagation();
        }}
        sx={{
          display: "inline-flex",
          alignItems: "center",
          cursor: "help",
          color: "text.disabled",
          verticalAlign: inline ? "-0.15em" : undefined,
          ml: inline ? 0.5 : 0,
          borderRadius: "50%",
          outline: "none",
          "&:focus-visible": { boxShadow: (t) => `0 0 0 2px ${t.palette.primary.main}` },
          ...sx,
        }}
      >
        <InfoOutlinedIcon fontSize="inherit" sx={{ fontSize: "1em" }} />
      </Box>
    </Tooltip>
  );
}

export function Blurb({
  children,
  more,
  moreLabel = "More",
  lessLabel = "Less",
  variant = "caption",
  sx,
}: {
  children: ReactNode;
  more?: ReactNode;
  moreLabel?: string;
  lessLabel?: string;
  variant?: "caption" | "body2";
  sx?: SxProps<Theme>;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <Box sx={{ mb: 1.5, ...sx }}>
      <Typography variant={variant} color="text.secondary" component="div">
        {children}
        {more != null && (
          <>
            {" "}
            <Link
              component="button"
              type="button"
              underline="hover"
              aria-expanded={open}
              aria-controls={id}
              onClick={() => setOpen((v) => !v)}
              sx={{ font: "inherit", verticalAlign: "baseline" }}
            >
              {open ? lessLabel : moreLabel}
            </Link>
          </>
        )}
      </Typography>
      {more != null && (
        <Collapse in={open} unmountOnExit>
          <Typography
            id={id}
            variant={variant}
            color="text.secondary"
            component="div"
            sx={{ mt: 0.75, pl: 1.5, borderLeft: 2, borderColor: "divider" }}
          >
            {more}
          </Typography>
        </Collapse>
      )}
    </Box>
  );
}

export function FoldCard({
  title,
  summary,
  icon,
  actions,
  defaultOpen = false,
  openWhen = false,
  children,
  sx,
}: {
  title: ReactNode;
  summary?: ReactNode;
  icon?: ReactNode;
  // Rendered in the header without toggling the fold (a button that runs something).
  actions?: ReactNode;
  defaultOpen?: boolean;
  // Opens the card when it turns true — for a header action whose result lands in the
  // body (a verify button, a fetch). It never closes the card: the user owns that.
  openWhen?: boolean;
  children: ReactNode;
  sx?: SxProps<Theme>;
}) {
  const [open, setOpen] = useState(defaultOpen || openWhen);
  const id = useId();
  useEffect(() => {
    if (openWhen) setOpen(true);
  }, [openWhen]);
  const toggle = () => setOpen((v) => !v);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };
  return (
    <Card sx={{ mb: 2, ...sx }}>
      <Box
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
        onKeyDown={onKey}
        sx={{
          px: 2,
          py: 1.5,
          display: "flex",
          alignItems: "center",
          gap: 1,
          cursor: "pointer",
          userSelect: "none",
          outline: "none",
          "&:hover": { bgcolor: "action.hover" },
          "&:focus-visible": { boxShadow: (t) => `inset 0 0 0 2px ${t.palette.primary.main}` },
        }}
      >
        {icon}
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography variant="h6" sx={{ lineHeight: 1.3 }}>
            {title}
          </Typography>
          {summary && (
            <Typography variant="caption" color="text.secondary" component="div">
              {summary}
            </Typography>
          )}
        </Box>
        {actions && (
          <Stack
            direction="row"
            spacing={1}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            {actions}
          </Stack>
        )}
        <ExpandMoreIcon
          aria-hidden
          sx={{
            color: "action.active",
            transform: open ? "rotate(180deg)" : "none",
            transition: "transform 150ms",
          }}
        />
      </Box>
      <Collapse in={open} unmountOnExit>
        <CardContent id={id} sx={{ pt: 0 }}>
          {children}
        </CardContent>
      </Collapse>
    </Card>
  );
}

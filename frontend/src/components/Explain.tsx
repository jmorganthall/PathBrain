import { useState, type ReactNode } from "react";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Collapse from "@mui/material/Collapse";
import IconButton from "@mui/material/IconButton";
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
      <InfoOutlinedIcon
        fontSize="inherit"
        sx={{
          fontSize: "1em",
          color: "text.disabled",
          cursor: "help",
          verticalAlign: inline ? "-0.15em" : undefined,
          ml: inline ? 0.5 : 0,
          ...sx,
        }}
      />
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
  children,
  sx,
}: {
  title: ReactNode;
  summary?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  sx?: SxProps<Theme>;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Card sx={{ mb: 2, ...sx }}>
      <Box
        onClick={() => setOpen((v) => !v)}
        sx={{
          px: 2,
          py: 1.5,
          display: "flex",
          alignItems: "center",
          gap: 1,
          cursor: "pointer",
          userSelect: "none",
          "&:hover": { bgcolor: "action.hover" },
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
          <Stack direction="row" spacing={1} onClick={(e) => e.stopPropagation()}>
            {actions}
          </Stack>
        )}
        <IconButton
          size="small"
          aria-label={open ? "collapse" : "expand"}
          sx={{ transform: open ? "rotate(180deg)" : "none", transition: "transform 150ms" }}
        >
          <ExpandMoreIcon />
        </IconButton>
      </Box>
      <Collapse in={open} unmountOnExit>
        <CardContent sx={{ pt: 0 }}>{children}</CardContent>
      </Collapse>
    </Card>
  );
}

import { useState } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Collapse from "@mui/material/Collapse";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";

interface Props {
  data: unknown;
  label?: string;
  defaultOpen?: boolean;
}

export default function JsonViewer({ data, label = "details", defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Box>
      <Button
        size="small"
        onClick={() => setOpen((v) => !v)}
        endIcon={open ? <ExpandLessIcon /> : <ExpandMoreIcon />}
        sx={{ textTransform: "none" }}
      >
        {open ? "Hide" : "Show"} {label}
      </Button>
      <Collapse in={open}>
        <Box
          component="pre"
          sx={{
            mt: 1,
            p: 1.5,
            borderRadius: 1.5,
            bgcolor: "rgba(0,0,0,0.35)",
            border: "1px solid rgba(255,255,255,0.06)",
            fontSize: 12,
            overflow: "auto",
            maxHeight: 320,
            m: 0,
            fontFamily: "monospace",
          }}
        >
          {JSON.stringify(data, null, 2)}
        </Box>
      </Collapse>
    </Box>
  );
}

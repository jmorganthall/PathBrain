import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import InboxOutlinedIcon from "@mui/icons-material/InboxOutlined";
import type { ReactNode } from "react";

interface Props {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: ReactNode;
}

export default function EmptyState({ title, description, action, icon }: Props) {
  return (
    <Box
      sx={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        textAlign: "center",
        py: 8,
        gap: 1.5,
        color: "text.secondary",
      }}
    >
      <Box sx={{ fontSize: 56, lineHeight: 1, color: "text.disabled" }}>
        {icon ?? <InboxOutlinedIcon fontSize="inherit" />}
      </Box>
      <Typography variant="h6" color="text.primary">
        {title}
      </Typography>
      {description && (
        <Typography variant="body2" sx={{ maxWidth: 420 }}>
          {description}
        </Typography>
      )}
      {action && <Box sx={{ mt: 2 }}>{action}</Box>}
    </Box>
  );
}

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";

import type { HostPort } from "../../api/types";
import { vHostOrIp, vPort } from "../../utils/validate";

interface Props {
  label: string;
  helperText?: string;
  items: HostPort[];
  onChange: (items: HostPort[]) => void;
  defaultPort?: number;
}

/** Edit a list of host:port targets with add/remove rows and validation. */
export default function HostPortListEditor({
  label,
  helperText,
  items,
  onChange,
  defaultPort = 443,
}: Props) {
  const update = (i: number, patch: Partial<HostPort>) => {
    const next = items.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const remove = (i: number) => onChange(items.filter((_, idx) => idx !== i));
  const add = () => onChange([...items, { host: "", port: defaultPort }]);

  return (
    <Box>
      <Typography variant="subtitle2">{label}</Typography>
      {helperText && (
        <Typography variant="caption" color="text.secondary">
          {helperText}
        </Typography>
      )}
      <Stack spacing={1} sx={{ mt: 1 }}>
        {items.map((item, i) => {
          const hostErr = vHostOrIp(item.host);
          const portErr = vPort(item.port);
          return (
            <Stack key={i} direction="row" spacing={1} alignItems="flex-start">
              <TextField
                size="small"
                label="Host"
                fullWidth
                value={item.host}
                onChange={(e) => update(i, { host: e.target.value })}
                error={Boolean(hostErr)}
                helperText={hostErr ?? undefined}
              />
              <TextField
                size="small"
                label="Port"
                type="number"
                value={Number.isFinite(item.port) ? item.port : ""}
                onChange={(e) => update(i, { port: parseInt(e.target.value, 10) })}
                error={Boolean(portErr)}
                helperText={portErr ?? undefined}
                sx={{ width: 130 }}
                inputProps={{ min: 1, max: 65535 }}
              />
              <IconButton aria-label="remove" onClick={() => remove(i)} sx={{ mt: 0.5 }}>
                <DeleteOutlineIcon fontSize="small" />
              </IconButton>
            </Stack>
          );
        })}
        {items.length === 0 && (
          <Typography variant="caption" color="text.secondary">
            None configured.
          </Typography>
        )}
        <Box>
          <Button size="small" startIcon={<AddIcon />} onClick={add}>
            Add target
          </Button>
        </Box>
      </Stack>
    </Box>
  );
}

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";

import type { DnsProvider } from "../../api/types";
import { vIpOrLocal } from "../../utils/validate";

interface Props {
  items: DnsProvider[];
  onChange: (items: DnsProvider[]) => void;
}

/** Edit the list of DNS resolvers (name + server IP, or 'local'). */
export default function DnsProviderListEditor({ items, onChange }: Props) {
  const update = (i: number, patch: Partial<DnsProvider>) => {
    const next = items.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const remove = (i: number) => onChange(items.filter((_, idx) => idx !== i));
  const add = () => onChange([...items, { name: "", server: "" }]);

  return (
    <Box>
      <Typography variant="subtitle2">Resolvers</Typography>
      <Typography variant="caption" color="text.secondary">
        Each resolver is queried for every hostname below. Use an IPv4/IPv6 address,
        or <code>local</code> for the system resolver.
      </Typography>
      <Stack spacing={1} sx={{ mt: 1 }}>
        {items.map((item, i) => {
          const serverErr = vIpOrLocal(item.server);
          return (
            <Stack key={i} direction="row" spacing={1} alignItems="flex-start">
              <TextField
                size="small"
                label="Name"
                value={item.name}
                onChange={(e) => update(i, { name: e.target.value })}
                sx={{ width: 200 }}
              />
              <TextField
                size="small"
                label="Server"
                fullWidth
                value={item.server}
                placeholder="1.1.1.1 or local"
                onChange={(e) => update(i, { server: e.target.value })}
                error={Boolean(serverErr)}
                helperText={serverErr ?? undefined}
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
            Add resolver
          </Button>
        </Box>
      </Stack>
    </Box>
  );
}

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";

interface Props {
  label: string;
  helperText?: string;
  items: string[];
  onChange: (items: string[]) => void;
  validate?: (value: string) => string | null;
  placeholder?: string;
  addLabel?: string;
}

/** Edit a list of strings with add/remove rows and per-row validation. */
export default function StringListEditor({
  label,
  helperText,
  items,
  onChange,
  validate,
  placeholder,
  addLabel = "Add",
}: Props) {
  const update = (i: number, value: string) => {
    const next = items.slice();
    next[i] = value;
    onChange(next);
  };
  const remove = (i: number) => onChange(items.filter((_, idx) => idx !== i));
  const add = () => onChange([...items, ""]);

  return (
    <Box>
      <Typography variant="subtitle2">{label}</Typography>
      {helperText && (
        <Typography variant="caption" color="text.secondary">
          {helperText}
        </Typography>
      )}
      <Stack spacing={1} sx={{ mt: 1 }}>
        {items.map((value, i) => {
          const err = validate ? validate(value) : null;
          return (
            <Stack key={i} direction="row" spacing={1} alignItems="flex-start">
              <TextField
                size="small"
                fullWidth
                value={value}
                placeholder={placeholder}
                onChange={(e) => update(i, e.target.value)}
                error={Boolean(err)}
                helperText={err ?? undefined}
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
            {addLabel}
          </Button>
        </Box>
      </Stack>
    </Box>
  );
}

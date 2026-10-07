import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KEY_CAPABILITIES, mintKey, type MintKeyResult } from "../api/keys";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface KeyMintDialogProps {
  team: string;
  onClose: () => void;
}

function MintForm({ team, onMinted }: { team: string; onMinted: (result: MintKeyResult) => void }) {
  const [label, setLabel] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [expiresAt, setExpiresAt] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      mintKey(team, {
        label,
        capabilities,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : undefined,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "keys"] });
      onMinted(result);
    },
  });

  function toggleCapability(cap: string) {
    setCapabilities((prev) => (prev.includes(cap) ? prev.filter((c) => c !== cap) : [...prev, cap]));
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <div className={styles.formField}>
        <label htmlFor="mint-label">Label</label>
        <input id="mint-label" value={label} onChange={(e) => setLabel(e.target.value)} required />
      </div>
      <div className={styles.formField}>
        <label>Capabilities</label>
        {KEY_CAPABILITIES.map((cap) => (
          <label key={cap} className={styles.checklistItem}>
            <input type="checkbox" checked={capabilities.includes(cap)} onChange={() => toggleCapability(cap)} />
            {cap}
          </label>
        ))}
      </div>
      <div className={styles.formField}>
        <label htmlFor="mint-expires">Expires at (optional)</label>
        <input id="mint-expires" type="datetime-local" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} />
      </div>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      <div className={styles.formActions}>
        <button type="submit" className={styles.buttonPrimary} disabled={!label.trim() || mutation.isPending}>
          {mutation.isPending ? "Minting..." : "Mint key"}
        </button>
      </div>
    </form>
  );
}

function MintedSecret({ result, onDone }: { result: MintKeyResult; onDone: () => void }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(result.key);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div>
      <p className={`${styles.badge} ${styles.warn}`} role="alert">
        This secret is shown once, right now. It is not written anywhere the console can retrieve again - closing
        this dialog loses it permanently.
      </p>
      <div className={styles.secretBox}>
        <span>{result.key}</span>
        <button type="button" className={styles.buttonSecondary} onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p>
        prefix: <code>{result.key_prefix}</code> | capabilities: {result.capabilities.join(", ")}
      </p>
      <div className={styles.formActions}>
        <button type="button" className={styles.buttonPrimary} onClick={onDone}>
          Done - I've copied it
        </button>
      </div>
    </div>
  );
}

export function KeyMintDialog({ team, onClose }: KeyMintDialogProps) {
  const [minted, setMinted] = useState<MintKeyResult | null>(null);

  return (
    <Modal title="Mint key" onClose={onClose}>
      {minted ? <MintedSecret result={minted} onDone={onClose} /> : <MintForm team={team} onMinted={setMinted} />}
    </Modal>
  );
}

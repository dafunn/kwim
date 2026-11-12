import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createSkill, reviseSkill } from "../api/skills";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

const TEMPLATE = `---
name:
description:
agents:
tags:
refs:
---

`;

interface SkillEditDialogProps {
  team: string;
  /** Revise this skill; omitted for a new one. */
  name?: string;
  initialText?: string;
  onClose: () => void;
}

/** Write a new skill or a revision. Either commits directly as the operator. */
export function SkillEditDialog({ team, name, initialText, onClose }: SkillEditDialogProps) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(initialText ?? TEMPLATE);
  const [owner, setOwner] = useState("");

  const save = useMutation({
    mutationFn: () => (name ? reviseSkill(team, name, text) : createSkill(team, text, owner || undefined)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "skills"] });
      onClose();
    },
  });

  return (
    <Modal title={name ? `Revise ${name}` : "New skill"} onClose={onClose}>
      <div className={styles.formField}>
        <label htmlFor="skill-text">SKILL.md</label>
        <textarea id="skill-text" rows={20} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
      </div>
      {!name && (
        <div className={styles.formField}>
          <label htmlFor="skill-owner">Owner (an operator; you when empty)</label>
          <input id="skill-owner" value={owner} onChange={(e) => setOwner(e.target.value)} />
        </div>
      )}
      {save.isError && <ErrorNotice error={save.error} />}
      <div className={styles.formActions}>
        <button type="button" className={styles.buttonSecondary} onClick={onClose}>
          Cancel
        </button>
        <button type="button" className={styles.buttonPrimary} disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving..." : name ? "Save revision" : "Create skill"}
        </button>
      </div>
    </Modal>
  );
}

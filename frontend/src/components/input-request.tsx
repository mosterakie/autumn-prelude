"use client";
import { useId, useState } from "react";
import type { InputRequest } from "@/lib/contracts";
import { Button, Notice } from "./ui";

export function InputRequestForm({
  request,
  now,
  onSubmit,
  onCancel,
}: {
  request: InputRequest;
  now: number;
  onSubmit: (answer: string) => Promise<void>;
  onCancel: () => Promise<void>;
}) {
  const group = useId(),
    [answer, setAnswer] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const options = request.options || [],
    expired = !!request.expires_at && Date.parse(request.expires_at) <= now,
    valid = options.length ? options.includes(answer) : !!answer.trim();
  return (
    <form
      className="inset form-stack"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || expired || !valid) return;
        setBusy(true);
        setError("");
        try {
          await onSubmit(answer);
        } catch (e) {
          setError((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {options.length ? (
        <fieldset
          className="input-options form-stack"
          disabled={busy || expired}
        >
          <legend>{request.prompt}</legend>
          <p className="small muted">请选择一项，不需要手动输入选项文字。</p>
          {options.map((option) => (
            <label className="checkbox" key={option}>
              <input
                type="radio"
                name={group}
                value={option}
                checked={answer === option}
                onChange={() => setAnswer(option)}
                required
              />
              {option}
            </label>
          ))}
        </fieldset>
      ) : (
        <label>
          {request.prompt}
          <textarea
            required
            maxLength={8000}
            rows={3}
            value={answer}
            disabled={busy || expired}
            onChange={(event) => setAnswer(event.target.value)}
          />
        </label>
      )}
      <p className="small muted">
        此处只补充信息。保存或公开内容仍需检查操作预览并点击“确认执行”。
      </p>
      {expired && <Notice>补充请求已过期，请停止本次任务后重新提问。</Notice>}
      {error && <Notice error>{error}</Notice>}
      <div className="row wrap">
        <Button
          type="submit"
          className="primary"
          busy={busy}
          disabled={!valid || expired}
        >
          补充并继续
        </Button>
        <Button
          type="button"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              await onCancel();
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          停止本次任务
        </Button>
      </div>
    </form>
  );
}

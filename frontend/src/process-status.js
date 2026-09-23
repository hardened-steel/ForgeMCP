/** Labels for the process protocol's current_status value. */
export function processStatus(value) {
  if (typeof value === "number") {
    return value === 0
      ? { label: "Completed successfully", tone: "fm-success" }
      : { label: `Exited with code ${value}`, tone: "fm-error" };
  }
  if (value === "running") return { label: "Running", tone: "fm-active" };
  if (value === "interrupted") return { label: "Interrupted by timeout", tone: "fm-error" };
  if (value === "stopped") return { label: "Stopped", tone: "fm-warning" };
  if (value === "stream_failure") return { label: "Stream failure", tone: "fm-error" };
  return { label: String(value), tone: "fm-warning" };
}

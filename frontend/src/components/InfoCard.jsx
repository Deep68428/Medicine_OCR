/**
 * A titled card container used to group related info rows.
 * @param {Object} props
 * @param {string} props.title - The card heading text.
 * @param {React.ReactNode} props.children - Content rendered inside the card.
 * @returns {JSX.Element}
 */
export function InfoCard({ title, children }) {
  return (
    <div className="app-card">
      <div className="app-card-title">{title}</div>
      {children}
    </div>
  );
}

/**
 * Renders a label-value row inside an InfoCard.
 * @param {Object} props
 * @param {string} props.label - The row label.
 * @param {*} props.value - The value to display; renders "—" when nullish.
 * @returns {JSX.Element}
 */
export function InfoRow({ label, value, valueClassName }) {
  return (
    <div className="app-info-row">
      <span className="app-info-label">{label}</span>
      <span className={`app-info-value${valueClassName ? ` ${valueClassName}` : ""}`}>{value ?? "—"}</span>
    </div>
  );
}

/**
 * Renders a label-value row that applies error styling when a value is present.
 * @param {Object} props
 * @param {string} props.label - The row label.
 * @param {string|null} props.value - The error message to display; applies error style when truthy.
 * @returns {JSX.Element}
 */
export function ErrorRow({ label, value }) {
  const valueClassName = value
    ? "app-info-value app-info-value-error"
    : "app-info-value app-info-value-muted";

  return (
    <div className="app-info-row">
      <span className="app-info-label">{label}</span>
      <span className={valueClassName}>{value ?? "—"}</span>
    </div>
  );
}

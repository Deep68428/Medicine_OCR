/**
 * A clickable statistics box displaying a label and numeric value with customizable colors.
 * @param {Object} props
 * @param {string} props.label - The stat label text.
 * @param {number} props.value - The stat value to display.
 * @param {string} props.bg - Background color of the box.
 * @param {string} props.labelColor - CSS color for the label text.
 * @param {string} props.valueColor - CSS color for the value text.
 * @param {string} props.border - CSS color for the box border.
 * @param {boolean} props.isActive - Whether this box is the currently active filter.
 * @param {function(): void} props.onClick - Callback fired when the box is clicked or activated.
 * @returns {JSX.Element}
 */
export function StatBox({
  label,
  value,
  bg,
  labelColor,
  valueColor,
  border,
  isActive,
  onClick,
}) {
  return (
    <div
      className={isActive ? "app-stat-box app-stat-box-active" : "app-stat-box"}
      style={{ backgroundColor: bg, border: `1.5px solid ${border}` }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      }}
    >
      <div className="app-stat-label" style={{ color: labelColor }}>
        {label}
      </div>
      <div className="app-stat-value" style={{ color: valueColor }}>
        {value}
      </div>
    </div>
  );
}

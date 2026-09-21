import { cx } from "../lib/format";

function splitFileName(fileName: string): [string, string] {
  const index = fileName.lastIndexOf(".");
  if (index <= 0 || index === fileName.length - 1) return [fileName, ""];
  return [fileName.slice(0, index), fileName.slice(index)];
}

export function FileName({
  value,
  className,
}: {
  value: string;
  className?: string;
}) {
  const [base, extension] = splitFileName(value);
  return (
    <span className={cx("file-name", className)} title={value}>
      <span className="file-name-base">{base}</span>
      {extension && <span className="file-name-extension">{extension}</span>}
    </span>
  );
}

export { splitFileName };

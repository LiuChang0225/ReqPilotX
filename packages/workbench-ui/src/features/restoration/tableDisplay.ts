/** Read a shared table presentation without promoting its old Map label to source text. */
export interface TableDisplaySource {
  title: string;
  agent_display_label?: string;
  presentation?: { display_title?: string; authority?: string; map_title?: string };
}

export function tableDisplayAuthority(table: TableDisplaySource): "AGENT_DISPLAY_LABEL" | undefined {
  return table.presentation?.authority === "AGENT_DISPLAY_LABEL" || table.agent_display_label
    ? "AGENT_DISPLAY_LABEL" : undefined;
}

export function tableDisplayTitle(table: TableDisplaySource): string {
  return tableDisplayAuthority(table)
    ? table.presentation?.display_title || table.agent_display_label || table.title
    : table.title;
}

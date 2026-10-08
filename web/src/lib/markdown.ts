import MarkdownIt from 'markdown-it'
import cjkFriendly from 'markdown-it-cjk-friendly'

/**
 * Markdown renderer for briefs and answers.
 *
 * - html: false - generated text is never rendered as raw HTML;
 * - cjk-friendly - CommonMark does not close `**…。**最近` (punctuation before the
 *   delimiter, a CJK letter after it), which LLM output does all the time;
 * - links open in a new tab without referrer.
 */
export function createMarkdown() {
  const md = new MarkdownIt({ html: false, linkify: true, typographer: false }).use(cjkFriendly)
  const defaultLink =
    md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
  md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
    const token = tokens[idx]
    if (token) {
      token.attrSet('target', '_blank')
      token.attrSet('rel', 'noopener noreferrer')
    }
    return defaultLink(tokens, idx, options, env, self)
  }
  return md
}

# Work a long page without reading all of it

A full interaction tree costs about 20 tokens per element: 2.7k tokens for the first 150
elements of Hacker News, and 10.5k for a whole Wikipedia article. Most of that is repetition,
and most tasks need one region.

## 1. Get the summary

```json
{"tool": "get_interaction_tree", "arguments": {"view": "summary"}}
```

```text
nav:Primary @7 | link: 12 Docs; 13 Blog; 14 Pricing; +40 more
form:login @31 | in: 32 user*; 33 password (password)* | btn: 34 Log in
main @40 | btn: 41 Add to cart x30 | link: 50 Item one; 51 Item two; +28 more
```

- Every entry starts with its id, and every id can be clicked straight away.
- `x30` folds thirty identical entries under the first one's id.
- `+N more` counts the entries the line does not show.
- `*` marks a required field.

## 2. Expand one region

The id after `@` belongs to the region's landmark element:

```json
{"tool": "get_interaction_tree", "arguments": {"within": "40"}}
```

`within` also takes a CSS selector (`"#results"`). Shadow roots and iframes inside the
element are included.

## 3. Or describe what you want

```json
{"tool": "find_elements", "arguments": {"description": "Add to cart button for Item two"}}
```

Write the words the page shows, plus the kind and region: "Sign in link in header", not
"the place where I log in". `ranker: "folate"` matches by meaning instead. It needs the
`folate` extra and takes seconds per call.

## Reading text

Read one region rather than the whole page:

```json
{"tool": "get_text_content", "arguments": {"selector": "main", "format": "markdown"}}
```

For a list of a hundred items across many pages, use the tree to find the right selectors
once. Then extract with one `execute_javascript` call per page, not one tool call per item.

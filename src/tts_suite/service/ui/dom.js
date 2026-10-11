// Builds elements without ever putting data into HTML text: what the service reports is only text.
export function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag)
  for (const [name, value] of Object.entries(attributes)) {
    if (value === false || value === null || value === undefined) continue
    if (name.startsWith('on')) element.addEventListener(name.slice(2), value)
    else if (name === 'value' || name === 'checked' || name === 'disabled' || name === 'selected') element[name] = value
    else element.setAttribute(name, value === true ? '' : value)
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue
    element.append(child instanceof Node ? child : document.createTextNode(String(child)))
  }
  return element
}

export function replaceChildren(parent, ...children) {
  parent.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false))
}

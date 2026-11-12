# Whether a source can be read during evaluation without building it.
src:
let
  path = if builtins.isAttrs src then src.outPath else src;
in
src != null
&& !(builtins.isAttrs src && src ? drvPath)
&& (
  !(builtins.isString path && builtins.hasContext path)
  || builtins.all (c: c.path or false) (builtins.attrValues (builtins.getContext path))
)

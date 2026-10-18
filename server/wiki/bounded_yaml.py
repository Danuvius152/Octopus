"""Bound metadata parsing before construction or alias-expanded serialization."""

import yaml

MAX_BYTES = 256 * 1024
MAX_DEPTH = 64
MAX_NODES = 10_000
MAX_ALIASES = 128
MAX_EXPANDED_BYTES = 2 * 1024 * 1024


class MetadataLoader(yaml.SafeLoader):
    def __init__(self, source):
        if len(source.encode("utf-8")) > MAX_BYTES:
            raise yaml.YAMLError("Frontmatter byte limit exceeded")
        self.metadata_depth = self.metadata_nodes = self.metadata_aliases = 0
        super().__init__(source)

    def compose_node(self, parent, index):
        self.metadata_nodes += 1
        self.metadata_depth += 1
        if self.check_event(yaml.AliasEvent):
            self.metadata_aliases += 1
        try:
            if (self.metadata_nodes > MAX_NODES or self.metadata_depth > MAX_DEPTH
                    or self.metadata_aliases > MAX_ALIASES):
                raise yaml.YAMLError("Frontmatter structure limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self.metadata_depth -= 1


# Dates remain their original source spelling, as in the document format.
MetadataLoader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))


def _budget(node):
    """Compute expanded cost on the DAG without expanding aliases ourselves."""
    visiting, costs = set(), {}

    def visit(current, depth):
        identity = id(current)
        if identity in visiting:
            raise yaml.YAMLError("Cyclic frontmatter alias")
        if depth > MAX_DEPTH:
            raise yaml.YAMLError("Frontmatter depth limit exceeded")
        if identity in costs:
            count, size, height = costs[identity]
            if depth + height > MAX_DEPTH:
                raise yaml.YAMLError("Frontmatter depth limit exceeded")
            return count, size, height
        visiting.add(identity)
        count, size, height = 1, 8, 0
        if isinstance(current, yaml.ScalarNode):
            size += len(current.value.encode("utf-8"))
        else:
            children = ([child for pair in current.value for child in pair]
                        if isinstance(current, yaml.MappingNode) else current.value)
            for child in children:
                child_count, child_size, child_height = visit(child, depth + 1)
                count += child_count
                size += child_size
                height = max(height, child_height + 1)
                if count > MAX_NODES or size > MAX_EXPANDED_BYTES:
                    raise yaml.YAMLError("Frontmatter alias expansion limit exceeded")
        visiting.remove(identity)
        costs[identity] = count, size, height
        return count, size, height

    if node is not None:
        visit(node, 1)


def _parse(source, *, construct):
    loader = MetadataLoader(source)
    try:
        node = loader.get_single_node()
        _budget(node)
        return loader.construct_document(node) if construct and node is not None else node
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise yaml.YAMLError("Unsupported frontmatter scalar or structure") from exc
    finally:
        loader.dispose()


def load(source):
    return _parse(source, construct=True)


def compose(source):
    return _parse(source, construct=False)

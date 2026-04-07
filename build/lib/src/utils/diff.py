from typing import Any, Dict, Set

class DictDiff:
    """Utility class for computing diffs between two dictionaries."""

    @staticmethod
    def compute(
        old: Dict[str, Any], 
        new: Dict[str, Any], 
        exclude: Set[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        Compute what changed between two snapshots.
        Returns a dictionary where each key maps to a dict with "before" and "after" values.
        
        Example:
            old = {"a": 1, "b": 2}
            new = {"a": 1, "b": 3}
            returns {"b": {"before": 2, "after": 3}}
        """
        exclude = exclude or set()
        diff = {}
        all_keys = set(old.keys()) | set(new.keys())
        
        for k in all_keys:
            if k in exclude:
                continue
                
            old_val = old.get(k)
            new_val = new.get(k)
            
            if old_val != new_val:
                diff[k] = {
                    "before": old_val,
                    "after": new_val
                }
        return diff

import importlib.util, sys
spec=importlib.util.spec_from_file_location("r","review.py"); r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
H=[{"id":"muse","cooldown_until":None},{"id":"cursor","cooldown_until":None},{"id":"claude","cooldown_until":None}]
def C(login,email,body="",name=""): return {"authors":[{"login":login,"email":email,"name":name}],"messageHeadline":"x","messageBody":body}
claude_c={"authors":[{"login":"example-user","email":"j@x"},{"login":"claude","email":"noreply@anthropic.com","name":"Claude"}],"messageHeadline":"fix","messageBody":"stuff\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"}
cursor_c=C("example-user","j@x","x\n\nCo-authored-by: Cursor <cursoragent@cursor.com>")
muse_c=C("example-user","j@x","x\n\nCo-authored-by: Muse Code <muse@meta.ai>")
human=C("alice","alice@example.com","No production change.")
human_claude=C("cdupont","claude@gmail.com","",name="Claude Dupont")
greptile_body="<!-- greptile_comment -->Reviewed by Greptile, written with Claude code <!-- /greptile_comment -->"
cases={
 "claude-authored":{"title":"t","body":greptile_body,"commits":[claude_c,claude_c]},
 "cursor-authored":{"title":"t","body":"","commits":[cursor_c]},
 "cursor-body-only":{"title":"t","body":"[Open in Cursor](https://cursor.com/agents?id=bc-1)","commits":[human]},
 "muse-authored":{"title":"t","body":"","commits":[muse_c]},
 "claude+cursor":{"title":"t","body":"","commits":[claude_c,cursor_c]},
 "unknown/human":{"title":"Update CLAUDE.md and cursor rules","body":"Edits CLAUDE.md for Claude","commits":[human]},
 "human named Claude":{"title":"t","body":"","commits":[human_claude]},
 "dependabot":{"title":"Bump","body":"","commits":[C("dependabot[bot]","x@users.noreply.github.com","Signed-off-by: dependabot[bot] <support@github.com>")]},
}
import copy
for name,meta in cases.items():
    av=r.detect_authoring_harnesses(meta)
    h,why=r.pick_harness(H,av,H,[])
    print(f"{name:20} avoid={sorted(av)}\n    harness pick: {h['id']} ({why})")
print("--- cooldown scenarios")
av=r.detect_authoring_harnesses(cases["muse-authored"])
H2=copy.deepcopy(H); H2[1]["cooldown_until"]="2026-11-02T19:00:00Z"
cand=[H2[0],H2[2]]
h,why=r.pick_harness(cand,av,H2,[]); print("muse-authored, cursor cooling:", h["id"], "|", why)
av=r.detect_authoring_harnesses(cases["claude+cursor"])
H3=copy.deepcopy(H); H3[0]["cooldown_until"]="2026-11-02T19:00:00Z"
h,why=r.pick_harness([H3[1],H3[2]],av,H3,[]); print("claude+cursor, muse cooling:", h["id"], "|", why)
h,why=r.pick_harness([H[1],H[2]],av,H,["muse"]); print("claude+cursor, muse failed:", h["id"], "|", why)
av=r.detect_authoring_harnesses(cases["cursor-authored"])
h,why=r.pick_harness([H[1],H[2]],av,H,["muse"]); print("cursor-authored, muse failed:", h["id"], "|", why)

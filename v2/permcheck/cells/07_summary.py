CHECK_DF = pd.DataFrame(CHECKS)
counts = CHECK_DF.Result.value_counts().to_dict()
print(" | ".join(f"{k}: {counts.get(k, 0)}" for k in ("PASS", "FAIL", "WARN", "INFO")))
problems = CHECK_DF[CHECK_DF.Result.isin(["FAIL", "WARN"])]
if problems.empty:
    print("Everything needed is in place.")
else:
    show(problems)
show(CHECK_DF, rows=500)

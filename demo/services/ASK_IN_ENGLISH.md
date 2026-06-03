# ASK IN ENGLISH — natural-language front door (`nl_query_service.py`, port 8910)

Type an English question; the service classifies it (local LLM, heuristic fallback),
writes the matching TaxiQL, runs it on Orbital `:9022`, and returns the answer.

```bash
python3 demo/services/nl_query_service.py   # binds 0.0.0.0:8910
```

How the web app calls it (vanilla JS, no dependencies):

```javascript
// "is Tesco in trouble?" -> company | "price of AAPL" -> ticker | "issues on orbitalapi/orbital" -> repo
const r = await fetch(`http://localhost:8910/ask?q=${encodeURIComponent(question)}`);
const { intent, target, taxiql, answer, summary } = await r.json();   // also: POST {"q": question}
document.querySelector("#answer").textContent = summary;              // one-line human answer
document.querySelector("#taxiql").textContent = taxiql;               // the query Orbital ran
```

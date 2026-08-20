
# Build
`pip install -r requirements.txt`

# TESTS
```
 python3 -m simulator.tests.test_option_pricer -v
 python3 -m simulator.tests.test_portfolio -v
 python3 -m simulator.tests.test_pmcc -v
 python -m simulator.tests.test_engine
 python -m simulator.tests.test_session_store
 ```

 # Run PMCC

Running a fully automated backtest
```
python -m simulator.run_backtest

python -m simulator.run_backtest --ticker AAPL --start 2024-01-01 --end 2024-12-31
```

Running an interactive session : Note you need to adapt this script to add your own entry / exit / adjust logic , here is the template:

```
python3 -m simulator.try_session
```

UI using Streamlit 

```
PYTHONPATH=. streamlit run simulator/ui/app.py
PYTHONPATH=. streamlit run simulator/ui/app.py --server.port 8080
#if you want it reachable from another machine on your network, not just localhost
PYTHONPATH=. streamlit run simulator/ui/app.py --server.port 8080 --server.address 0.0.0.0
#stops it from trying to auto-open a browser tab (handy if you're running it somewhere without a display, e.g. a remote server)
PYTHONPATH=. streamlit run simulator/ui/app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
```


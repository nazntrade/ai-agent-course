from harness.runner import tests
if __name__=="__main__":
    result=tests("integration")
    if result==0:
        from harness.stub_startup import main
        result=main()
    raise SystemExit(result)

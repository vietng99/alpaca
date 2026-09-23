import sys
def main(argv):
    print("hello " + (argv[1] if len(argv) > 1 else "world"))
if __name__ == "__main__":
    main(sys.argv)

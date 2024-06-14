.PHONY: setup setup-huggingface-cli setup-model-repo download-model build build-cli test clean-package-caches

PIP_COMMAND := pip3
PYTHON_COMMAND := python3

# Define model repository and directories
MODEL_REPO_DIR := ./models/

setup:
	@echo "Setting up environment..."
	@which $(PIP_COMMAND)
	@which $(PYTHON_COMMAND)
	@echo "Checking for Homebrew..."
	@which brew > /dev/null || (echo "Error: Homebrew is not installed. Install it form here https://brew.sh and try again" && exit 1)
	@echo "Homebrew is installed."
	@echo "Checking for huggingface-cli..."
	@which huggingface-cli > /dev/null || (echo "Installing huggingface-cli..." && brew install huggingface-cli)
	@echo "huggingface-cli is installed."
	@echo "Checking for git-lfs..."
	@which git-lfs > /dev/null || (echo "Installing git-lfs..." && brew install git-lfs)
	@echo "git-lfs is installed."
	@echo "Checking for trash..."
	@which trash > /dev/null || (echo "Installing trash..." && brew install trash)
	@echo "trash is installed."
	@echo "Done 🚀"


# Download a specific model
download-model:
	@if [ -z "$(MODEL)" ]; then \
		echo "Error: MODEL is not set. Usage: make download-model MODEL=your-model-repo"; \
		exit 1; \
	fi
	@echo "Setting up repository..."
	@mkdir -p $(MODEL_REPO_DIR)
	@if [ -d "$(MODEL_REPO_DIR)/$(MODEL)/.git" ]; then \
		echo "Repository exists, resetting..."; \
		export GIT_LFS_SKIP_SMUDGE=1; \
		cd $(MODEL_REPO_DIR)/$(MODEL) && git fetch --all && git reset --hard origin/main && git clean -fdx; \
	else \
		echo "Repository not found, initializing..."; \
		export GIT_LFS_SKIP_SMUDGE=1; \
		git clone https://huggingface.co/$(MODEL) $(MODEL_REPO_DIR)/$(MODEL); \
	fi
	@echo "Downloading model $(MODEL)..."
	@cd $(MODEL_REPO_DIR)/$(MODEL) && \
	git lfs pull --include="*"

clean-package-caches:
	@trash ~/Library/Caches/org.swift.swiftpm/repositories
	@trash ~/Library/Developer/Xcode/DerivedData
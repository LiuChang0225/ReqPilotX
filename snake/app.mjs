import {
  DIRECTIONS,
  createInitialState,
  queueDirection,
  restartGame,
  stepGame,
  togglePause,
} from "./logic.mjs";

const TICK_MS = 140;
const GRID_SIZE = 16;

const boardElement = document.querySelector("[data-board]");
const scoreElement = document.querySelector("[data-score]");
const statusElement = document.querySelector("[data-status]");
const restartButton = document.querySelector("[data-restart]");
const pauseButton = document.querySelector("[data-pause]");
const controlButtons = document.querySelectorAll("[data-control]");

const directionMap = {
  up: DIRECTIONS.UP,
  down: DIRECTIONS.DOWN,
  left: DIRECTIONS.LEFT,
  right: DIRECTIONS.RIGHT,
  arrowup: DIRECTIONS.UP,
  arrowdown: DIRECTIONS.DOWN,
  arrowleft: DIRECTIONS.LEFT,
  arrowright: DIRECTIONS.RIGHT,
  w: DIRECTIONS.UP,
  s: DIRECTIONS.DOWN,
  a: DIRECTIONS.LEFT,
  d: DIRECTIONS.RIGHT,
};

let state = createInitialState({ gridSize: GRID_SIZE });
const cells = [];

function toCellIndex(position, gridSize) {
  return position.y * gridSize + position.x;
}

function createBoard(gridSize) {
  boardElement.innerHTML = "";
  boardElement.style.setProperty("--grid-size", String(gridSize));

  const fragment = document.createDocumentFragment();
  for (let i = 0; i < gridSize * gridSize; i += 1) {
    const cell = document.createElement("div");
    cell.className = "cell";
    cells.push(cell);
    fragment.appendChild(cell);
  }

  boardElement.appendChild(fragment);
}

function render() {
  for (const cell of cells) {
    cell.classList.remove("snake", "head", "food");
  }

  for (let i = 0; i < state.snake.length; i += 1) {
    const segment = state.snake[i];
    const index = toCellIndex(segment, state.gridSize);
    const cell = cells[index];
    if (!cell) {
      continue;
    }

    cell.classList.add("snake");
    if (i === 0) {
      cell.classList.add("head");
    }
  }

  if (state.food) {
    const foodIndex = toCellIndex(state.food, state.gridSize);
    const foodCell = cells[foodIndex];
    if (foodCell) {
      foodCell.classList.add("food");
    }
  }

  scoreElement.textContent = String(state.score);

  if (state.gameOver) {
    statusElement.textContent = "Game over";
  } else if (state.paused) {
    statusElement.textContent = "Paused";
  } else {
    statusElement.textContent = "Running";
  }

  pauseButton.textContent = state.paused ? "Resume" : "Pause";
}

function requestDirection(direction) {
  state = queueDirection(state, direction);
  render();
}

function tick() {
  state = stepGame(state);
  render();
}

createBoard(state.gridSize);
render();
setInterval(tick, TICK_MS);

restartButton.addEventListener("click", () => {
  state = restartGame(state);
  render();
});

pauseButton.addEventListener("click", () => {
  state = togglePause(state);
  render();
});

for (const button of controlButtons) {
  button.addEventListener("click", () => {
    const control = button.getAttribute("data-control");
    if (!control) {
      return;
    }

    const direction = directionMap[control];
    if (direction) {
      requestDirection(direction);
    }
  });
}

document.addEventListener("keydown", (event) => {
  const key = event.key.toLowerCase();
  if (key === " " || key === "p") {
    event.preventDefault();
    state = togglePause(state);
    render();
    return;
  }

  const direction = directionMap[key];
  if (!direction) {
    return;
  }

  event.preventDefault();
  requestDirection(direction);
});

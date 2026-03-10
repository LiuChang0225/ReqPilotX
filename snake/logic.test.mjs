import test from "node:test";
import assert from "node:assert/strict";

import {
  DIRECTIONS,
  queueDirection,
  spawnFood,
  stepGame,
} from "./logic.mjs";

function makeState(overrides = {}) {
  return {
    gridSize: 6,
    snake: [
      { x: 2, y: 2 },
      { x: 1, y: 2 },
      { x: 0, y: 2 },
    ],
    direction: { ...DIRECTIONS.RIGHT },
    pendingDirection: null,
    food: { x: 5, y: 5 },
    score: 0,
    gameOver: false,
    paused: false,
    ...overrides,
  };
}

test("stepGame moves snake one cell and keeps length when not eating", () => {
  const next = stepGame(makeState());

  assert.deepEqual(next.snake, [
    { x: 3, y: 2 },
    { x: 2, y: 2 },
    { x: 1, y: 2 },
  ]);
  assert.equal(next.score, 0);
  assert.equal(next.gameOver, false);
});

test("stepGame grows snake, increments score, and respawns food when eating", () => {
  const state = makeState({
    snake: [
      { x: 1, y: 1 },
      { x: 0, y: 1 },
    ],
    food: { x: 2, y: 1 },
  });

  const next = stepGame(state, () => 0);

  assert.equal(next.score, 1);
  assert.deepEqual(next.snake, [
    { x: 2, y: 1 },
    { x: 1, y: 1 },
    { x: 0, y: 1 },
  ]);
  assert.deepEqual(next.food, { x: 0, y: 0 });
});

test("stepGame sets gameOver when snake hits wall", () => {
  const state = makeState({
    snake: [{ x: 5, y: 0 }],
    direction: { ...DIRECTIONS.RIGHT },
  });

  const next = stepGame(state);
  assert.equal(next.gameOver, true);
});

test("stepGame sets gameOver when snake hits itself", () => {
  const state = makeState({
    snake: [
      { x: 2, y: 2 },
      { x: 2, y: 3 },
      { x: 1, y: 3 },
      { x: 1, y: 2 },
      { x: 1, y: 1 },
    ],
    direction: { ...DIRECTIONS.LEFT },
  });

  const next = stepGame(state);
  assert.equal(next.gameOver, true);
});

test("queueDirection blocks immediate reverse turns and only stores one queued turn", () => {
  const state = makeState();

  const reverse = queueDirection(state, DIRECTIONS.LEFT);
  assert.equal(reverse, state);

  const first = queueDirection(state, DIRECTIONS.UP);
  assert.deepEqual(first.pendingDirection, { x: 0, y: -1 });

  const second = queueDirection(first, DIRECTIONS.LEFT);
  assert.deepEqual(second.pendingDirection, { x: 0, y: -1 });
});

test("spawnFood returns null when no free cells remain", () => {
  const fullSnake = [
    { x: 0, y: 0 },
    { x: 1, y: 0 },
    { x: 0, y: 1 },
    { x: 1, y: 1 },
  ];

  const food = spawnFood(fullSnake, 2, () => 0);
  assert.equal(food, null);
});
